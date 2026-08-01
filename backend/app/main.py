from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.orchestrator.langgraph_flow import initialize_graph, shutdown_graph
from app.routes.chat import router as chat_router
from app.services.chat_store import initialize_chat_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_chat_store()
    await initialize_graph()
    try:
        yield
    finally:
        await shutdown_graph()


app = FastAPI(title="Multi-Agent Orchestrator", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/api")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
