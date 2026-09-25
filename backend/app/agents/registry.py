"""Agent Registry.

Agents are declared with an `AgentSpec` and registered by capability. The
router and planner only reason about capabilities; the registry resolves which
agents provide them. Adding an agent means adding one module in `app/agents/`
that calls `register_agent(...)` - no routing code changes are required, and a
brand-new capability can be introduced through `routing_hints`.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import RETRYABLE_TYPES, ErrorType

if TYPE_CHECKING:
    from app.agents.context import AgentContext
    from app.models import AgentResult, AgentTask

AgentHandler = Callable[["AgentTask", "AgentContext"], Awaitable["AgentResult"]]


class RetryPolicy(BaseModel):
    max_retries: int = Field(default=1, ge=0, le=5)
    backoff_base_seconds: float = Field(default=0.25, ge=0)
    backoff_max_seconds: float = Field(default=4.0, ge=0)
    jitter: bool = True
    retry_on: set[ErrorType] = Field(default_factory=lambda: set(RETRYABLE_TYPES))

    def should_retry(self, error_type: ErrorType, retryable: bool, attempt: int) -> bool:
        return retryable and error_type in self.retry_on | {ErrorType.MODEL_ERROR} and attempt <= self.max_retries

    def delay(self, attempt: int) -> float:
        """Exponential backoff with (full-range half) jitter; attempt starts at 1."""
        base = min(self.backoff_max_seconds, self.backoff_base_seconds * (2 ** (attempt - 1)))
        return base * random.uniform(0.5, 1.0) if self.jitter else base


class ModelPolicy(BaseModel):
    tier: Literal["none", "fast", "quality"] = "quality"
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_output_tokens: int = Field(default=1200, ge=1, le=16000)


class PermissionPolicy(BaseModel):
    granted_permissions: set[str] = Field(default_factory=set)
    data_scope: Literal["own_user", "own_project"] = "own_project"


class RoutingHints(BaseModel):
    """Optional hints that let an agent introduce a new capability without
    touching the router. Keys are capability names."""

    description: str = ""
    keywords: dict[str, float] = Field(default_factory=dict)
    file_extensions: set[str] = Field(default_factory=set)
    url_patterns: list[str] = Field(default_factory=list)
    suppresses: set[str] = Field(default_factory=set)


class AgentSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    capabilities: list[str]
    handler: Callable[..., Any] = Field(exclude=True)  # AgentHandler
    tools: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=45, gt=0, le=600)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    model_policy: ModelPolicy = Field(default_factory=ModelPolicy)
    token_budget: int = Field(default=6000, ge=0)
    permission_policy: PermissionPolicy = Field(default_factory=PermissionPolicy)
    category: Literal["task", "system", "output"] = "task"
    user_selectable: bool = True
    priority: int = 100  # lower wins when several agents share a capability
    routing_hints: dict[str, RoutingHints] = Field(default_factory=dict)
    availability: Callable[[], tuple[bool, str]] | None = Field(default=None, exclude=True)

    def is_available(self) -> tuple[bool, str]:
        if self.availability is None:
            return True, "available"
        try:
            return self.availability()
        except Exception as exc:  # availability probes must never crash routing
            return False, f"availability check failed ({exc.__class__.__name__})"

    def describe(self) -> dict[str, Any]:
        available, status = self.is_available()
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "tools": self.tools,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": self.retry_policy.model_dump(mode="json"),
            "model_policy": self.model_policy.model_dump(mode="json"),
            "token_budget": self.token_budget,
            "permissions": sorted(self.permission_policy.granted_permissions),
            "category": self.category,
            "user_selectable": self.user_selectable,
            "available": available,
            "status": status,
        }


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}
        self._lock = threading.Lock()

    def register(self, spec: AgentSpec, *, replace: bool = False) -> AgentSpec:
        with self._lock:
            if spec.name in self._agents and not replace:
                raise ValueError(f"Agent '{spec.name}' is already registered.")
            self._agents[spec.name] = spec
        return spec

    def get(self, name: str) -> AgentSpec:
        spec = self._agents.get(name)
        if spec is None:
            raise KeyError(f"Unknown agent: {name}")
        return spec

    def has(self, name: str) -> bool:
        return name in self._agents

    def all(self, category: str | None = None) -> list[AgentSpec]:
        return [spec for spec in self._agents.values() if category is None or spec.category == category]

    def find_by_capability(self, capability: str, *, include_unavailable: bool = False) -> list[AgentSpec]:
        matches = [
            spec
            for spec in self._agents.values()
            if capability in spec.capabilities and (include_unavailable or spec.is_available()[0])
        ]
        return sorted(matches, key=lambda spec: (spec.priority, spec.name))

    def capabilities(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for spec in self._agents.values():
            for capability in spec.capabilities:
                mapping.setdefault(capability, []).append(spec.name)
        return mapping

    def routing_hints(self) -> dict[str, RoutingHints]:
        merged: dict[str, RoutingHints] = {}
        for spec in self._agents.values():
            for capability, hints in spec.routing_hints.items():
                current = merged.setdefault(capability, RoutingHints(description=hints.description))
                current.keywords.update(hints.keywords)
                current.file_extensions |= hints.file_extensions
                current.url_patterns.extend(hints.url_patterns)
                current.suppresses |= hints.suppresses
        return merged

    @contextmanager
    def override(self, name: str, handler: AgentHandler | Callable[..., Any], **changes: Any) -> Iterator[AgentSpec]:
        """Temporarily replace an agent's handler (tests, evaluations)."""
        original = self.get(name)
        replacement = original.model_copy(update={"handler": handler, **changes})
        with self._lock:
            self._agents[name] = replacement
        try:
            yield replacement
        finally:
            with self._lock:
                self._agents[name] = original


agent_registry = AgentRegistry()


def register_agent(**spec_fields: Any) -> Callable[[AgentHandler], AgentHandler]:
    """Decorator: `@register_agent(name=..., capabilities=[...], ...)`."""

    def decorator(handler: AgentHandler) -> AgentHandler:
        agent_registry.register(AgentSpec(handler=handler, **spec_fields), replace=True)
        return handler

    return decorator
