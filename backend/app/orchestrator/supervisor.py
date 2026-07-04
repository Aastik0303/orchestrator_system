from app.agents.code_dev import code_dev_agent
from app.agents.data_analyst import data_analyst_agent
from app.agents.document_rag import document_rag_agent
from app.agents.general_chat import deep_research_agent, general_chat_agent
from app.agents.youtube_rag import youtube_rag_agent
from app.models import AgentName, AgentResponse, ChatRequest
from app.orchestrator.router import choose_agent

AGENTS = {
    AgentName.GENERAL_CHAT: general_chat_agent,
    AgentName.DEEP_RESEARCH: deep_research_agent,
    AgentName.DOCUMENT_RAG: document_rag_agent,
    AgentName.YOUTUBE_RAG: youtube_rag_agent,
    AgentName.CODE_DEV: code_dev_agent,
    AgentName.DATA_ANALYST: data_analyst_agent,
}


def dispatch(request: ChatRequest) -> AgentResponse:
    active_agent = choose_agent(request)
    handler = AGENTS[active_agent]
    return handler(request)

