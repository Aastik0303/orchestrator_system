from app.models import AgentName, AgentResponse, ChatRequest


def youtube_rag_agent(request: ChatRequest) -> AgentResponse:
    return AgentResponse(
        active_agent=AgentName.YOUTUBE_RAG,
        response=(
            "YouTube RAG Agent active. This route is ready for transcript extraction, "
            "summary generation, and question answering over video content."
        ),
    )

