from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentName(str, Enum):
    AUTO = "auto"
    GENERAL_CHAT = "general_chat"
    DEEP_RESEARCH = "deep_research"
    DOCUMENT_RAG = "document_rag"
    YOUTUBE_RAG = "youtube_rag"
    CODE_DEV = "code_dev"
    DATA_ANALYST = "data_analyst"


class UploadedFile(BaseModel):
    name: str
    content_type: str | None = None
    storage_path: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    deep_research: bool = False
    agent_override: AgentName = AgentName.AUTO
    files: list[UploadedFile] = Field(default_factory=list)


class AgentResponse(BaseModel):
    active_agent: AgentName
    response: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: bool = False

