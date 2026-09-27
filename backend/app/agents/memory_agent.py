from __future__ import annotations

from app.agents.context import AgentContext
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, register_agent
from app.memory.service import request_digest
from app.models import AgentResult, AgentTask

MAX_MEMORIES = 4


@register_agent(
    name="memory",
    description="Retrieves the caller's scoped long-term memory relevant to the request.",
    capabilities=["memory_retrieval"],
    category="system",
    user_selectable=False,
    tools=["vector.search_memory"],
    timeout_seconds=15,
    retry_policy=RetryPolicy(max_retries=0),
    model_policy=ModelPolicy(tier="none"),
    token_budget=0,
    permission_policy=PermissionPolicy(granted_permissions={"memory:read"}),
)
async def memory_agent(task: AgentTask, ctx: AgentContext) -> AgentResult:
    memories = await ctx.call_tool("vector.search_memory", {"query": task.goal[:4000], "limit": MAX_MEMORIES + 1})
    # A memory recorded by an earlier run of this same request says nothing
    # new about it, and injecting it would make a repeated request see a
    # different prompt than its first run.
    current = request_digest(task.goal)
    compact = [
        {"id": item.get("id"), "content": item.get("content", "")[:500], "similarity": round(float(item.get("similarity", 0.0)), 3)}
        for item in memories
        if float(item.get("similarity", 0.0)) >= 0.2 and (item.get("metadata") or {}).get("request_digest") != current
    ][:MAX_MEMORIES]
    return AgentResult(
        summary=f"Retrieved {len(compact)} scoped memory records.",
        metadata={"memories": compact, "tool_success": True},
    )
