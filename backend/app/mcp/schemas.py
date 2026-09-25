from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolPermission(str, Enum):
    ALLOWED = "allowed"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    server: str
    description: str
    permission: ToolPermission
    risk_level: RiskLevel = RiskLevel.LOW
    required_permissions: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    status: str = "available"
    read_only: bool = True
    input_model: type[BaseModel] | None = Field(default=None, exclude=True)
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"

    @property
    def input_schema(self) -> dict[str, Any]:
        if self.input_model is None:
            return {"type": "object"}
        return self.input_model.model_json_schema()

    @property
    def requires_approval(self) -> bool:
        return self.permission == ToolPermission.APPROVAL_REQUIRED or self.risk_level in {
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        }


class ToolContext(BaseModel):
    user_id: str
    project_id: str
    run_id: str | None = None
    agent: str | None = None
    approved_tools: set[str] = Field(default_factory=set)


class HumanApprovalRequired(PermissionError):
    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"Tool {tool_name} requires human approval.")
        self.tool_name = tool_name
        self.arguments = arguments
