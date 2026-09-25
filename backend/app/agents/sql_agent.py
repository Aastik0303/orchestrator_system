from __future__ import annotations

from app.agents.context import AgentContext
from app.agents.registry import ModelPolicy, register_agent
from app.models import AgentResult, AgentTask


@register_agent(
    name="sql_agent",
    description="Produces or executes allowlisted read-only SQL workflows (execution disabled until configured).",
    capabilities=["sql_query"],
    tools=[],
    timeout_seconds=20,
    model_policy=ModelPolicy(tier="none"),
    token_budget=0,
)
async def sql_agent(task: AgentTask, ctx: AgentContext) -> AgentResult:
    return AgentResult(
        summary="The SQL request was routed correctly, but no approved read-only database connection is configured.",
        warnings=[
            "SQL execution remains disabled until a scoped read-only connection and schema allowlist are configured."
        ],
        metadata={"tool_success": False},
    )
