from __future__ import annotations

from app.agents.common import build_user_prompt, provider_unavailable
from app.agents.context import AgentContext
from app.agents.prompts import system_prompt
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, register_agent
from app.models import AgentResult, AgentTask

ROLE = (
    "You are the Code Development Agent. Help with architecture, implementation, "
    "debugging, review, and testing. Be concrete and preserve user data. You cannot "
    "execute code or shell commands; say so if asked."
)


@register_agent(
    name="code_dev",
    description="Handles implementation, architecture, debugging, and review.",
    capabilities=["code_generation"],
    tools=["github.read_repository", "github.search_code"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.2, max_output_tokens=1800),
    token_budget=8000,
    permission_policy=PermissionPolicy(granted_permissions={"github:read"}),
)
async def code_dev(task: AgentTask, ctx: AgentContext) -> AgentResult:
    response = await ctx.llm(system=system_prompt(ROLE), user=build_user_prompt(task))
    if response is None:
        return provider_unavailable("Code analysis")
    return AgentResult(summary=response.text, metadata={"model": response.model, "tokens": response.total_tokens})
