import re
from pathlib import Path

from app.models import AgentName, ChatRequest

YOUTUBE_RE = re.compile(r"(youtube\.com/watch\?v=|youtu\.be/)", re.IGNORECASE)
CODE_HINTS = {
    "api",
    "bug",
    "code",
    "debug",
    "fastapi",
    "function",
    "javascript",
    "langchain",
    "langgraph",
    "machine learning",
    "ml",
    "python",
    "react",
    "sql",
}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".md", ".txt"}
DATA_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def choose_agent(request: ChatRequest) -> AgentName:
    if request.agent_override != AgentName.AUTO:
        return request.agent_override

    file_extensions = {Path(file.name).suffix.lower() for file in request.files}

    if file_extensions & DATA_EXTENSIONS:
        return AgentName.DATA_ANALYST

    if file_extensions & DOCUMENT_EXTENSIONS:
        return AgentName.DOCUMENT_RAG

    if YOUTUBE_RE.search(request.message):
        return AgentName.YOUTUBE_RAG

    message = request.message.lower()
    if any(hint in message for hint in CODE_HINTS):
        return AgentName.CODE_DEV

    if request.deep_research:
        return AgentName.DEEP_RESEARCH

    return AgentName.GENERAL_CHAT

