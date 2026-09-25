from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentName(str, Enum):
    """Names of the built-in agents.

    Agent identifiers are plain strings validated against the Agent Registry,
    so new agents do not need to be added here. This enum only gives the
    built-in names stable constants.
    """

    AUTO = "auto"

    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    MEMORY = "memory"

    GENERAL_CHAT = "general_chat"
    DEEP_RESEARCH = "deep_research"
    DOCUMENT_RAG = "document_rag"
    YOUTUBE_RAG = "youtube_rag"
    CODE_DEV = "code_dev"
    DATA_ANALYST = "data_analyst"
    SQL_AGENT = "sql_agent"
    PYTHON_EXECUTOR = "python_executor"

    REPORT_GENERATOR = "report_generator"
    EVALUATION = "evaluation"


SYSTEM_AGENTS = {
    AgentName.SUPERVISOR.value,
    AgentName.PLANNER.value,
    AgentName.MEMORY.value,
    AgentName.REPORT_GENERATOR.value,
    AgentName.EVALUATION.value,
}


def agent_value(agent: AgentName | str | None) -> str | None:
    if agent is None:
        return None
    return agent.value if isinstance(agent, AgentName) else str(agent)


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


TERMINAL_STEP_STATUSES = {
    StepStatus.SUCCESS,
    StepStatus.FAILED,
    StepStatus.BLOCKED,
    StepStatus.CANCELLED,
    StepStatus.TIMEOUT,
}


class UploadedFile(BaseModel):
    name: str
    content_type: str | None = None
    storage_path: str | None = None
    document_id: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    deep_research: bool = False
    agent_override: str = AgentName.AUTO.value
    session_id: str | None = None
    user_id: str = "local-user"
    project_id: str = "default"
    files: list[UploadedFile] = Field(default_factory=list)
    # Tools the user explicitly approved for this request (e.g. sandbox execution).
    approved_tools: list[str] = Field(default_factory=list)


class SourceReference(BaseModel):
    title: str | None = None
    source_type: str
    uri: str | None = None
    document_id: str | None = None
    page: int | None = None
    chunk_id: str | None = None


class AgentResult(BaseModel):
    summary: str
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingDecision(BaseModel):
    intent: str = "conversation"
    required_capabilities: list[str] = Field(default_factory=list)
    candidate_agents: list[str] = Field(default_factory=list)
    primary_agent: str
    secondary_agents: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reason: str
    requires_planning: bool = False
    strategy: Literal["override", "rule", "capability", "llm", "fallback"] = "capability"
    scores: dict[str, float] = Field(default_factory=dict)


class PlanStep(BaseModel):
    id: str
    node_type: Literal["agent", "tool", "logic", "output"]
    agent: str | None = None
    capability: str | None = None
    tool: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    description: str
    instruction: str | None = None
    parallel_group: str | None = None
    retry_limit: int = Field(default=1, ge=0, le=5)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    # all_success: every dependency must succeed; all_done: run once all
    # dependencies are terminal (optional inputs); any_success: at least one.
    dependency_mode: Literal["all_success", "all_done", "any_success"] = "all_success"


class ExecutionPlan(BaseModel):
    goal: str
    steps: list[PlanStep]
    strategy: str = "rules"


class StepArtifact(BaseModel):
    """Structured, size-bounded output of an upstream step passed downstream
    instead of raw conversation text."""

    step_id: str
    agent: str
    status: str
    summary: str
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTask(BaseModel):
    step_id: str
    agent: str
    capability: str | None = None
    goal: str
    instruction: str
    files: list[UploadedFile] = Field(default_factory=list)
    inputs: dict[str, StepArtifact] = Field(default_factory=dict)
    memory: list[dict[str, Any]] = Field(default_factory=list)
    conversation: list[dict[str, str]] = Field(default_factory=list)
    user_id: str = "local-user"
    project_id: str = "default"
    session_id: str | None = None
    deep_research: bool = False

    def request(self) -> ChatRequest:
        """Compatibility view for helpers that still accept a ChatRequest."""
        return ChatRequest(
            message=self.goal[:50_000] or "(empty)",
            user_id=self.user_id,
            project_id=self.project_id,
            session_id=self.session_id,
            files=self.files,
            deep_research=self.deep_research,
        )


class GuardrailFinding(BaseModel):
    category: str
    severity: Literal["low", "medium", "high"]
    detail: str


class GuardrailVerdict(BaseModel):
    stage: Literal["input", "output", "retrieval", "tool", "memory"]
    action: Literal["allow", "block", "redact"]
    findings: list[GuardrailFinding] = Field(default_factory=list)
    message: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == "block"


class EvaluationResult(BaseModel):
    overall_score: float = Field(ge=0, le=100)
    correctness: float = Field(ge=0, le=100)
    relevance: float = Field(ge=0, le=100)
    completeness: float = Field(ge=0, le=100)
    groundedness: float = Field(ge=0, le=100)
    hallucination_risk: Literal["low", "medium", "high"]
    tool_success_rate: float = Field(ge=0, le=100)
    format_valid: bool
    retry_recommended: bool
    feedback: list[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    active_agent: str
    response: str
    status: str = "completed"
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: bool = False
    route: RoutingDecision | None = None
    evaluation: EvaluationResult | None = None
    failures: list[dict[str, Any]] = Field(default_factory=list)
    guardrails: list[GuardrailVerdict] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    trace_id: str | None = None


class SupervisorDecision(BaseModel):
    action: str
    next_agents: list[str]
    reason: str
    terminate: bool = False


class WorkflowEvent(BaseModel):
    type: str
    run_id: str
    timestamp: str
    node_id: str | None = None
    parent_node_id: str | None = None
    node_type: str | None = None
    label: str | None = None
    status: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
