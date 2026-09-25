"""Per-run and per-step execution context.

`RunContext` holds everything scoped to ONE workflow run (identity, budget,
telemetry, cancellation). `AgentContext` binds it to one agent step and is the
only way an agent reaches models and tools, so every call is budgeted,
traced and permission-checked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.agents.registry import AgentSpec
from app.core.budget import Budget
from app.core.errors import RunCancelled
from app.llm.client import LLMClient, LLMResponse, llm_client
from app.mcp.registry import MCPRegistry, mcp_registry
from app.mcp.schemas import ToolContext
from app.models import GuardrailVerdict
from app.observability.telemetry import Telemetry
from app.services.runtime_store import RuntimeStore, runtime_store


@dataclass
class RunContext:
    run_id: str
    user_id: str
    project_id: str
    session_id: str | None
    trace_id: str
    budget: Budget
    telemetry: Telemetry
    approved_tools: frozenset[str] = frozenset()
    store: RuntimeStore = field(default=runtime_store)
    llm: LLMClient = field(default=llm_client)
    tools: MCPRegistry = field(default=mcp_registry)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    guardrail_verdicts: list[GuardrailVerdict] = field(default_factory=list)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise RunCancelled("Run was cancelled.")


@dataclass
class AgentContext:
    run: RunContext
    spec: AgentSpec
    step_id: str
    tokens_used: int = 0

    async def llm(
        self,
        *,
        system: str,
        user: str,
        name: str | None = None,
        max_tokens: int | None = None,
        cacheable: bool = False,
        json_mode: bool = False,
    ) -> LLMResponse | None:
        self.run.check_cancelled()
        policy = self.spec.model_policy
        if policy.tier == "none":
            return None
        remaining_agent_budget = max(1, self.spec.token_budget - self.tokens_used) if self.spec.token_budget else None
        requested = min(filter(None, [max_tokens or policy.max_output_tokens, remaining_agent_budget]))
        response = await self.run.llm.complete(
            system=system,
            user=user,
            tier=policy.tier,
            max_tokens=requested,
            temperature=policy.temperature,
            name=name or self.spec.name,
            budget=self.run.budget,
            cacheable=cacheable,
            json_mode=json_mode,
        )
        if response is not None:
            self.tokens_used += response.total_tokens
        return response

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        self.run.check_cancelled()
        self.run.budget.use_tool_call()
        result = await self.run.tools.execute(
            tool_name=tool_name,
            arguments=arguments,
            context=ToolContext(
                user_id=self.run.user_id,
                project_id=self.run.project_id,
                run_id=self.run.run_id,
                agent=self.spec.name,
                approved_tools=set(self.run.approved_tools),
            ),
            agent=self.spec,
        )
        return result["result"]
