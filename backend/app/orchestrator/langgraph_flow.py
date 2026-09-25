"""Public orchestration facade used by routes and workers."""

from __future__ import annotations

import asyncio

from app.agents.catalog import load_agents
from app.models import AgentResponse, ChatRequest
from app.orchestrator.executor import orchestrator


async def initialize_graph():
    load_agents()
    return orchestrator


async def shutdown_graph() -> None:
    return None


async def run_orchestrator(
    request: ChatRequest,
    *,
    run_id: str | None = None,
    cancel_event: asyncio.Event | None = None,
    watch_cancellation: bool = False,
) -> AgentResponse:
    return await orchestrator.run(
        request, run_id=run_id, cancel_event=cancel_event, watch_cancellation=watch_cancellation
    )
