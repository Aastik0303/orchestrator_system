from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.models import AgentName, AgentResponse, ChatRequest
from app.orchestrator.router import choose_agent
from app.orchestrator.supervisor import AGENTS

MAX_RETRIES = 2
VALID_AGENT_NAMES = set(AGENTS)


class OrchestratorState(TypedDict, total=False):
    request: ChatRequest
    active_agent: AgentName | None
    response: AgentResponse | None
    is_valid: bool
    retry_count: int
    error: str | None


_compiled_graph: Any | None = None
_postgres_context: AbstractAsyncContextManager[Any] | None = None


def _safe_error(exc: Exception) -> str:
    message = str(exc).replace(get_settings().groq_api_key or "", "[redacted]")
    return f"{exc.__class__.__name__}: {message}"[:500]


def _fallback_response(state: OrchestratorState) -> AgentResponse:
    active_agent = state.get("active_agent")
    if active_agent not in VALID_AGENT_NAMES:
        active_agent = AgentName.GENERAL_CHAT

    error = state.get("error")
    detail = f" Last error: {error}" if error else ""
    return AgentResponse(
        active_agent=active_agent,
        response=(
            "I could not complete that request through the selected agent after retrying. "
            "Please adjust the request or try again."
            f"{detail}"
        ),
    )


async def router_node(state: OrchestratorState) -> OrchestratorState:
    try:
        active_agent = choose_agent(state["request"])
    except Exception as exc:
        return {
            "active_agent": None,
            "response": None,
            "is_valid": False,
            "error": _safe_error(exc),
        }

    if active_agent not in VALID_AGENT_NAMES:
        return {
            "active_agent": active_agent,
            "response": None,
            "is_valid": False,
            "error": f"Invalid agent selection: {active_agent}",
        }

    return {
        "active_agent": active_agent,
        "response": None,
        "is_valid": False,
        "retry_count": 0,
        "error": None,
    }


def _make_agent_node(agent_name: AgentName) -> Callable[[OrchestratorState], Awaitable[OrchestratorState]]:
    async def agent_node(state: OrchestratorState) -> OrchestratorState:
        handler = AGENTS[agent_name]
        try:
            response = await asyncio.to_thread(handler, state["request"])
        except Exception as exc:
            return {"response": None, "is_valid": False, "error": _safe_error(exc)}

        return {
            "active_agent": agent_name,
            "response": response,
            "is_valid": False,
            "error": None,
        }

    return agent_node


async def validation_node(state: OrchestratorState) -> OrchestratorState:
    response = state.get("response")
    active_agent = state.get("active_agent")

    if response is None:
        return {"is_valid": False, "error": state.get("error") or "Agent returned no response."}

    if not response.response.strip():
        return {"is_valid": False, "error": "Agent returned an empty response."}

    if active_agent not in VALID_AGENT_NAMES or response.active_agent not in VALID_AGENT_NAMES:
        return {
            "is_valid": False,
            "error": f"Agent returned an invalid active_agent: {response.active_agent}",
        }

    if response.active_agent != active_agent:
        return {
            "is_valid": False,
            "error": (
                "Agent response active_agent did not match selected agent: "
                f"{response.active_agent} != {active_agent}"
            ),
        }

    return {"is_valid": True, "error": None}


async def retry_node(state: OrchestratorState) -> OrchestratorState:
    return {
        "retry_count": state.get("retry_count", 0) + 1,
        "response": None,
        "is_valid": False,
    }


async def fallback_node(state: OrchestratorState) -> OrchestratorState:
    return {"response": _fallback_response(state), "is_valid": True}


async def final_response_node(state: OrchestratorState) -> OrchestratorState:
    response = state.get("response") or _fallback_response(state)
    return {"response": response, "is_valid": True}


def route_from_router(state: OrchestratorState) -> str:
    active_agent = state.get("active_agent")
    if active_agent not in VALID_AGENT_NAMES:
        return "fallback"
    return active_agent.value


def route_after_validation(state: OrchestratorState) -> str:
    if state.get("is_valid"):
        return "final_response"
    if state.get("retry_count", 0) < MAX_RETRIES:
        return "retry"
    return "fallback"


def route_retry(state: OrchestratorState) -> str:
    active_agent = state.get("active_agent")
    if active_agent not in VALID_AGENT_NAMES:
        return "fallback"
    return active_agent.value


def build_graph(checkpointer: Any | None = None) -> Any:
    graph = StateGraph(OrchestratorState)

    graph.add_node("router", router_node)
    for agent_name in VALID_AGENT_NAMES:
        graph.add_node(agent_name.value, _make_agent_node(agent_name))
    graph.add_node("validation", validation_node)
    graph.add_node("retry", retry_node)
    graph.add_node("fallback", fallback_node)
    graph.add_node("final_response", final_response_node)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        route_from_router,
        {agent.value: agent.value for agent in VALID_AGENT_NAMES} | {"fallback": "fallback"},
    )
    for agent_name in VALID_AGENT_NAMES:
        graph.add_edge(agent_name.value, "validation")
    graph.add_conditional_edges(
        "validation",
        route_after_validation,
        {"final_response": "final_response", "retry": "retry", "fallback": "fallback"},
    )
    graph.add_conditional_edges(
        "retry",
        route_retry,
        {agent.value: agent.value for agent in VALID_AGENT_NAMES} | {"fallback": "fallback"},
    )
    graph.add_edge("fallback", END)
    graph.add_edge("final_response", END)

    return graph.compile(checkpointer=checkpointer)


async def initialize_graph() -> Any:
    global _compiled_graph, _postgres_context

    if _compiled_graph is not None:
        return _compiled_graph

    settings = get_settings()
    if settings.postgres_url:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:
            raise RuntimeError(
                "POSTGRES_URL is set, but langgraph-checkpoint-postgres is not installed."
            ) from exc

        _postgres_context = AsyncPostgresSaver.from_conn_string(settings.postgres_url)
        checkpointer = await _postgres_context.__aenter__()
        await checkpointer.setup()
    else:
        checkpointer = InMemorySaver()

    _compiled_graph = build_graph(checkpointer=checkpointer)
    return _compiled_graph


async def shutdown_graph() -> None:
    global _compiled_graph, _postgres_context

    if _postgres_context is not None:
        await _postgres_context.__aexit__(None, None, None)
    _postgres_context = None
    _compiled_graph = None


async def run_orchestrator(request: ChatRequest) -> AgentResponse:
    session_id = request.session_id or uuid4().hex
    graph = await initialize_graph()
    initial_state: OrchestratorState = {
        "request": request.model_copy(update={"session_id": session_id}),
        "active_agent": None,
        "response": None,
        "is_valid": False,
        "retry_count": 0,
        "error": None,
    }
    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
    )
    return result["response"]
