from typing import TypedDict

from langgraph.graph import END, StateGraph

from app.models import AgentName, AgentResponse, ChatRequest
from app.orchestrator.supervisor import dispatch


class OrchestratorState(TypedDict):
    request: ChatRequest
    active_agent: AgentName | None
    response: AgentResponse | None


def supervisor_node(state: OrchestratorState) -> OrchestratorState:
    response = dispatch(state["request"])
    return {
        "request": state["request"],
        "active_agent": response.active_agent,
        "response": response,
    }


def build_graph():
    graph = StateGraph(OrchestratorState)
    graph.add_node("supervisor", supervisor_node)
    graph.set_entry_point("supervisor")
    graph.add_edge("supervisor", END)
    return graph.compile()

