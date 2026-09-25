from __future__ import annotations

from app.agents.common import build_user_prompt, provider_unavailable
from app.agents.context import AgentContext
from app.agents.prompts import system_prompt
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.core.errors import OrchestratorError
from app.models import AgentResult, AgentTask

GENERAL_ROLE = "You are the General Chat Agent. Answer clearly and helpfully."
RESEARCH_ROLE = (
    "You are the Deep Research Agent. Distinguish sourced facts from inference and "
    "state clearly when live web tools are unavailable."
)


@register_agent(
    name="general_chat",
    description="Answers conversational requests that need no specialist.",
    capabilities=["conversation"],
    timeout_seconds=40,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.4, max_output_tokens=1000),
    token_budget=4000,
)
async def general_chat(task: AgentTask, ctx: AgentContext) -> AgentResult:
    response = await ctx.llm(system=system_prompt(GENERAL_ROLE), user=build_user_prompt(task))
    if response is None:
        return provider_unavailable("General Chat")
    return AgentResult(summary=response.text, metadata={"model": response.model, "tokens": response.total_tokens})


@register_agent(
    name="deep_research",
    description="Synthesizes research with explicit source limitations; uses web search when configured.",
    capabilities=["web_research"],
    tools=["web.web_search", "web.fetch_page"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.3, max_output_tokens=1400),
    token_budget=6000,
    permission_policy=PermissionPolicy(granted_permissions={"web:read"}),
    routing_hints={
        "web_research": RoutingHints(
            description="Research current information, markets, competitors or trends.",
            url_patterns=[r"https?://(?!(?:www\.)?(?:youtube\.com|youtu\.be))"],
        )
    },
)
async def deep_research(task: AgentTask, ctx: AgentContext) -> AgentResult:
    warnings: list[str] = []
    evidence = ""
    tool = ctx.run.tools.get("web.web_search")
    if tool is not None and tool.status == "available":
        try:
            results = await ctx.call_tool("web.web_search", {"query": task.goal[:500]})
            evidence = str(results)[:4000]
        except OrchestratorError as exc:
            warnings.append(f"Web search failed ({exc.error_type.value}); answering without live sources.")
    else:
        warnings.append("No live web source was used: the web search tool is not configured.")
    prompt = build_user_prompt(task)
    if evidence:
        from app.agents.prompts import wrap_untrusted

        prompt += "\n\n" + wrap_untrusted("document", evidence, source="web_search")
    response = await ctx.llm(system=system_prompt(RESEARCH_ROLE), user=prompt)
    if response is None:
        result = provider_unavailable("Deep research")
        result.warnings.extend(warnings)
        return result
    return AgentResult(
        summary=response.text,
        warnings=warnings,
        metadata={"model": response.model, "tokens": response.total_tokens, "live_sources": bool(evidence)},
    )
