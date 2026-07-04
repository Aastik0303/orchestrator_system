from app.models import AgentName, AgentResponse, ChatRequest
from app.services.groq_client import groq_completion


def general_chat_agent(request: ChatRequest) -> AgentResponse:
    response = groq_completion(
        "You are the General Chat Agent in a multi-agent orchestrator. Answer clearly and helpfully.",
        request.message,
    )
    return AgentResponse(
        active_agent=AgentName.GENERAL_CHAT,
        response=response
        or (
            "General Chat Agent active. I can answer conversational questions, "
            "route specialized work, and hand off to Deep Research when enabled. "
            "Add GROQ_API_KEY to .env to enable model responses."
        ),
    )


def deep_research_agent(request: ChatRequest) -> AgentResponse:
    response = groq_completion(
        (
            "You are the Deep Research Agent. Give comprehensive answers and explicitly "
            "state when live web access has not been connected yet."
        ),
        request.message,
    )
    return AgentResponse(
        active_agent=AgentName.DEEP_RESEARCH,
        response=response
        or (
            "Deep Research Agent active. The web research adapter is ready to be "
            "connected to a scraping/search provider for current, cited answers. "
            "Add GROQ_API_KEY to .env to enable model responses."
        ),
    )
