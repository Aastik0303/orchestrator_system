"""Tool permission guardrail.

A tool call is allowed only if ALL of the following hold:
1. the tool is not BLOCKED and is not CRITICAL risk;
2. when an agent is calling, the tool is on that agent's explicit allowlist and
   the agent holds every permission the tool requires;
3. approval-required or HIGH-risk tools were explicitly approved by the user for
   this request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.mcp.schemas import HumanApprovalRequired, RiskLevel, ToolContext, ToolDefinition, ToolPermission

if TYPE_CHECKING:
    from app.agents.registry import AgentSpec


class ToolPermissionDenied(PermissionError):
    pass


def enforce_permission(
    tool: ToolDefinition,
    arguments: dict[str, Any],
    context: ToolContext,
    agent: "AgentSpec | None" = None,
) -> None:
    qualified_name = tool.qualified_name
    if tool.permission == ToolPermission.BLOCKED or tool.risk_level == RiskLevel.CRITICAL:
        raise ToolPermissionDenied(f"Tool execution is blocked: {qualified_name}.")
    if agent is not None:
        if qualified_name not in agent.tools:
            raise ToolPermissionDenied(
                f"Agent '{agent.name}' is not permitted to use tool {qualified_name}."
            )
        missing = set(tool.required_permissions) - set(agent.permission_policy.granted_permissions)
        if missing:
            raise ToolPermissionDenied(
                f"Agent '{agent.name}' lacks permissions {sorted(missing)} for {qualified_name}."
            )
    if tool.requires_approval and qualified_name not in context.approved_tools:
        raise HumanApprovalRequired(qualified_name, arguments)
