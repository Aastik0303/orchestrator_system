"""Per-run execution budgets.

A `Budget` is created per workflow run (never shared between runs) and is the
single place where step, tool-call, token, LLM-call, retry and wall-clock
limits are enforced. Exhausting any limit raises `BudgetExceeded`, which the
execution engine treats as a safe, non-retryable stop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.core.errors import BudgetExceeded


@dataclass(frozen=True)
class ExecutionLimits:
    max_steps: int
    max_runtime_seconds: float
    max_tool_calls: int
    max_tokens: int
    max_retries: int
    max_llm_calls: int
    max_parallel_steps: int

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ExecutionLimits":
        settings = settings or get_settings()
        return cls(
            max_steps=settings.max_plan_nodes,
            max_runtime_seconds=float(settings.max_execution_seconds),
            max_tool_calls=settings.max_tool_calls,
            max_tokens=settings.max_tokens_per_run,
            max_retries=settings.max_retries_per_run,
            max_llm_calls=settings.max_llm_calls_per_run,
            max_parallel_steps=max(1, settings.max_parallel_steps),
        )


@dataclass
class Budget:
    limits: ExecutionLimits
    started_at: float = field(default_factory=time.monotonic)
    steps_started: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    retries: int = 0
    llm_calls: int = 0

    # All mutations happen on the event loop thread (agents running in worker
    # threads report usage back through async wrappers), so plain integer
    # updates are race-free.

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.limits.max_runtime_seconds - self.elapsed_seconds)

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.limits.max_tokens - self.tokens_used)

    def check_runtime(self) -> None:
        if self.remaining_seconds <= 0:
            raise BudgetExceeded(
                f"Run exceeded the maximum runtime of {self.limits.max_runtime_seconds:.0f}s.",
                details={"limit": "max_runtime"},
            )

    def start_step(self) -> None:
        self.check_runtime()
        if self.steps_started >= self.limits.max_steps:
            raise BudgetExceeded(
                f"Run exceeded the maximum of {self.limits.max_steps} steps.",
                details={"limit": "max_steps"},
            )
        self.steps_started += 1

    def use_tool_call(self) -> None:
        self.check_runtime()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExceeded(
                f"Run exceeded the maximum of {self.limits.max_tool_calls} tool calls.",
                details={"limit": "max_tool_calls"},
            )
        self.tool_calls += 1

    def reserve_llm_call(self, requested_tokens: int) -> int:
        """Reserve an LLM call; returns the max output tokens allowed for it."""
        self.check_runtime()
        if self.llm_calls >= self.limits.max_llm_calls:
            raise BudgetExceeded(
                f"Run exceeded the maximum of {self.limits.max_llm_calls} LLM calls.",
                details={"limit": "max_llm_calls"},
            )
        if self.remaining_tokens <= 0:
            raise BudgetExceeded(
                f"Run exhausted its token budget of {self.limits.max_tokens}.",
                details={"limit": "max_tokens"},
            )
        self.llm_calls += 1
        return max(1, min(requested_tokens, self.remaining_tokens))

    def record_tokens(self, tokens: int) -> None:
        self.tokens_used += max(0, int(tokens))

    def use_retry(self) -> bool:
        """Consume one retry from the run-wide pool. Returns False if exhausted."""
        if self.retries >= self.limits.max_retries:
            return False
        self.retries += 1
        return True

    def snapshot(self) -> dict[str, float | int]:
        return {
            "steps": self.steps_started,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens_used,
            "retries": self.retries,
            "llm_calls": self.llm_calls,
            "elapsed_ms": int(self.elapsed_seconds * 1000),
        }
