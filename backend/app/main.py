from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents.catalog import load_agents
from app.config import get_settings
from app.llm.client import llm_client
from app.mcp.registry import mcp_registry
from app.monitoring.logging import configure_logging, request_logger
from app.orchestrator.langgraph_flow import initialize_graph, shutdown_graph
from app.routes.chat import router as chat_router
from app.routes.runtime import router as runtime_router
from app.routes.websocket import router as websocket_router
from app.security.auth import validate_auth_configuration
from app.services.runtime_store import dispose_engines, runtime_store

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("orchestrator.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_auth_configuration()
    await asyncio.to_thread(runtime_store.initialize)
    load_agents()
    await initialize_graph()
    # Warm the routing catalog so the first request does not pay for it.
    from app.orchestrator.router import router

    router.catalog()
    if get_settings().auth_mode == "dev":
        logger.warning("auth_mode_dev: caller-supplied user ids are trusted; do not expose this deployment")
    try:
        yield
    finally:
        from app.orchestrator.executor import orchestrator

        await orchestrator.drain_background()
        await shutdown_graph()
        dispose_engines()


app = FastAPI(title="AI Agent Orchestration Platform", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-User-Id", "X-Request-Id", "traceparent"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


app.middleware("http")(request_logger)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    # Never leak stack traces or internal messages to clients.
    logger.exception("unhandled_exception", extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


app.include_router(chat_router, prefix="/api")
app.include_router(runtime_router, prefix="/api")
app.include_router(websocket_router)


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up. Does not touch dependencies."""
    current = get_settings()
    return {
        "status": "ok",
        "database_adapter": runtime_store.dialect,
        "redis": "configured" if current.redis_url else "not_configured",
        "llm_provider": current.llm_provider if llm_client.available() else "unavailable",
        "worker_mode": current.worker_mode,
        "auth_mode": current.auth_mode,
        "mcp_servers": len(mcp_registry.capabilities()["servers"]),
        "agents": len(load_agents().all()),
    }


@app.get("/ready")
async def ready():
    """Readiness: dependencies needed to serve traffic are reachable."""

    def check_database() -> None:
        with runtime_store.begin() as connection:
            connection.execute(sa.text("SELECT 1"))

    try:
        await asyncio.to_thread(check_database)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": exc.__class__.__name__})
    return {"status": "ready", "database": "ok"}
