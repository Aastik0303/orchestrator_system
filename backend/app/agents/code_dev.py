from app.models import AgentName, AgentResponse, ChatRequest
from app.services.groq_client import groq_completion


def code_dev_agent(request: ChatRequest) -> AgentResponse:
    response = groq_completion(
        (
            "You are the Code & Development Agent. Help with software engineering, "
            "debugging, Python, ML pipelines, and rapid prototypes. Be practical and precise."
        ),
        request.message,
    )
    return AgentResponse(
        active_agent=AgentName.CODE_DEV,
        response=response
        or (
            "Code & Development Agent active. I can handle implementation plans, "
            "debugging, Python generation, ML pipeline guidance, and prototypes. "
            "Add GROQ_API_KEY to .env to enable model responses."
        ),
    )
