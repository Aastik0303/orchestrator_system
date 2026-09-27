from __future__ import annotations

import re

from app.agents.common import build_user_prompt, provider_unavailable, render_evidence
from app.agents.context import AgentContext
from app.agents.prompts import system_prompt
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.core.errors import OrchestratorError
from app.guardrails.retrieval_guard import validate_citations
from app.mcp.web import extract_urls
from app.models import AgentResult, AgentTask, SourceReference

GENERAL_ROLE = "You are the General Chat Agent. Answer clearly and helpfully."
RESEARCH_ROLE = (
    "You are the Deep Research Agent. Distinguish sourced facts from inference and "
    "state clearly when live web tools are unavailable. When <document> sources are "
    "provided, cite every claim taken from them as [Source N] and never invent sources."
)
# Handled by the YouTube and code agents instead of page fetching.
NOT_WEB_PAGES_RE = re.compile(r"(?:youtube\.com|youtu\.be|github\.com)", re.IGNORECASE)
MAX_FETCHED_PAGES = 2


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
    description="Researches with live sources: reads linked pages and uses web search when configured, with citations.",
    capabilities=["web_research"],
    tools=["web.web_search", "web.fetch_page"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.3, max_output_tokens=1400),
    token_budget=8000,
    permission_policy=PermissionPolicy(granted_permissions={"web:read"}),
    routing_hints={
        "web_research": RoutingHints(
            description="Research current information, markets, competitors or trends.",
            url_patterns=[r"https?://(?!(?:www\.)?(?:youtube\.com|youtu\.be|github\.com))"],
        )
    },
)
async def deep_research(task: AgentTask, ctx: AgentContext) -> AgentResult:
    warnings: list[str] = []
    items: list[dict] = []
    tools = ctx.run.tools
    urls = extract_urls(task.goal, exclude=NOT_WEB_PAGES_RE, limit=MAX_FETCHED_PAGES)
    if urls and not tools.is_available("web.fetch_page"):
        warnings.append("Linked pages were not read: page fetching is disabled (WEB_FETCH_ENABLED).")
    elif urls:
        for url in urls:
            try:
                page = await ctx.call_tool("web.fetch_page", {"url": url, "max_chars": 8000})
                items.append({"title": page.get("title") or url, "url": page["url"], "content": page.get("content", "")})
            except (OrchestratorError, PermissionError) as exc:
                warnings.append(f"Could not read {url} ({exc.__class__.__name__}: {str(exc)[:120]}).")
    if tools.is_available("web.web_search"):
        query = " ".join(re.sub(r"https?://\S+", " ", task.instruction or task.goal).split())[:400] or task.goal[:400]
        try:
            for result in await ctx.call_tool("web.web_search", {"query": query, "max_results": 5}):
                items.append({"title": result.get("title") or result["url"], "url": result["url"], "content": result.get("content", "")})
        except OrchestratorError as exc:
            warnings.append(f"Web search failed ({exc.error_type.value}); answering without search results.")
    elif not items:
        warnings.append("No live web source was used: web search credentials or the Google engine ID are not configured.")

    evidence, kept, evidence_warnings = render_evidence(items, ctx)
    warnings.extend(evidence_warnings)
    prompt = build_user_prompt(task)
    if evidence:
        prompt += "\n\nLive sources:\n" + evidence
    response = await ctx.llm(system=system_prompt(RESEARCH_ROLE), user=prompt)
    sources = [SourceReference(title=item.get("title"), source_type="web", uri=item.get("url")) for item in kept]
    if response is None:
        result = provider_unavailable("Deep research")
        result.warnings.extend(warnings)
        result.sources = sources
        return result
    answer = response.text
    if kept:
        answer, citation_warnings = validate_citations(answer, len(kept))
        warnings.extend(citation_warnings)
    return AgentResult(
        summary=answer,
        sources=sources,
        warnings=list(dict.fromkeys(warnings)),
        metadata={
            "model": response.model,
            "tokens": response.total_tokens,
            "live_sources": len(kept),
            "tool_success": bool(kept) or not (urls or tools.is_available("web.web_search")),
        },
    )
