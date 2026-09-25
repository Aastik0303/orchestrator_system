from __future__ import annotations

from app.agents.context import AgentContext
from app.agents.registry import ModelPolicy, PermissionPolicy, RoutingHints, register_agent
from app.models import AgentResult, AgentTask


@register_agent(
    name="youtube_rag",
    description="Answers from retrieved video transcripts when a transcript provider is configured.",
    capabilities=["youtube_transcript"],
    tools=["web.fetch_page"],
    timeout_seconds=30,
    model_policy=ModelPolicy(tier="quality"),
    permission_policy=PermissionPolicy(granted_permissions={"web:read"}),
    routing_hints={
        "youtube_transcript": RoutingHints(
            description="Answer questions about a YouTube video.",
            url_patterns=[r"(?:youtube\.com/watch\?v=|youtu\.be/)"],
        )
    },
)
async def youtube_rag(task: AgentTask, ctx: AgentContext) -> AgentResult:
    return AgentResult(
        summary="A YouTube URL was detected, but no transcript provider is configured.",
        warnings=["Connect a transcript-capable MCP tool before this agent can retrieve video content."],
        metadata={"tool_success": False},
    )
