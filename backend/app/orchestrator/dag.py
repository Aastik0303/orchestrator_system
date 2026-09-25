"""Safe DAG execution engine.

* Ready steps (all dependencies terminal and the dependency mode satisfied)
  start immediately and run concurrently, bounded by `max_parallel_steps`.
* Each attempt has a timeout: min(step/agent timeout, remaining run budget).
* Failures are classified; only retryable ones are retried, with exponential
  backoff + jitter, bounded by the agent's retry policy AND the run-wide retry
  budget.
* Explicit states: PENDING, RUNNING, RETRYING, SUCCESS, FAILED, TIMEOUT,
  BLOCKED, CANCELLED. Dependents of failed steps are BLOCKED (never silently
  skipped). Security/approval denials are BLOCKED.
* Budgets: max steps, max runtime, tool calls, tokens, LLM calls, retries.
  Exhaustion stops the run safely.
* Cancellation: the run's cancel event (set locally or by the cross-replica DB
  watcher) cancels running steps and marks pending steps CANCELLED.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.context import AgentContext, RunContext
from app.agents.registry import AgentRegistry
from app.core.errors import (
    BudgetExceeded,
    ErrorType,
    ModelError,
    StructuredFailure,
    classify_exception,
)
from app.mcp.schemas import HumanApprovalRequired
from app.models import (
    TERMINAL_STEP_STATUSES,
    AgentResult,
    AgentTask,
    ExecutionPlan,
    PlanStep,
    StepArtifact,
    StepStatus,
)
from app.orchestrator.events import EventSink
from app.services.runtime_store import utc_now

logger = logging.getLogger("orchestrator.dag")

TaskFactory = Callable[[PlanStep, dict[str, StepArtifact]], AgentTask]


@dataclass
class StepState:
    step: PlanStep
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: AgentResult | None = None
    failure: StructuredFailure | None = None
    started_monotonic: float | None = None
    latency_ms: int = 0
    events: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STEP_STATUSES

    def artifact(self) -> StepArtifact:
        result = self.result or AgentResult(summary=self.failure.message if self.failure else "")
        return StepArtifact(
            step_id=self.step.id,
            agent=self.step.agent or "",
            status=self.status.value,
            summary=result.summary,
            findings=result.findings,
            recommendations=result.recommendations,
            sources=result.sources,
            artifacts=result.artifacts,
            warnings=result.warnings,
            metadata=result.metadata,
        )


def _label(agent: str | None) -> str:
    return agent.replace("_", " ").title() if agent else "Workflow Node"


class DagExecutor:
    CANCEL_POLL_SECONDS = 0.5

    def __init__(self, registry: AgentRegistry, sink: EventSink) -> None:
        self.registry = registry
        self.sink = sink

    async def execute(self, plan: ExecutionPlan, ctx: RunContext, task_factory: TaskFactory) -> dict[str, StepState]:
        states = {step.id: StepState(step=step) for step in plan.steps}
        for state in states.values():
            self.sink.step(
                state.step.id,
                agent=state.step.agent,
                status=StepStatus.PENDING.value,
                attempts=0,
                depends_on=state.step.depends_on,
            )
        semaphore = asyncio.Semaphore(ctx.budget.limits.max_parallel_steps)
        running: dict[asyncio.Task[None], str] = {}

        while True:
            if ctx.cancel_event.is_set():
                await self._cancel_all(states, running, StepStatus.CANCELLED, "Run was cancelled.", ErrorType.CANCELLED)
                break
            if ctx.budget.remaining_seconds <= 0:
                await self._cancel_all(
                    states, running, StepStatus.TIMEOUT, "Run exceeded its maximum runtime.", ErrorType.TIMEOUT
                )
                break

            for state in states.values():
                if state.status != StepStatus.PENDING:
                    continue
                readiness = self._readiness(state, states)
                if readiness == "wait":
                    continue
                if readiness == "blocked":
                    failed_deps = [
                        dep for dep in state.step.depends_on if states[dep].status != StepStatus.SUCCESS
                    ]
                    self._finish(
                        state,
                        StepStatus.BLOCKED,
                        failure=StructuredFailure(
                            agent=state.step.agent,
                            step_id=state.step.id,
                            status=StepStatus.BLOCKED.value.lower(),
                            error_type=ErrorType.PERMANENT,
                            retryable=False,
                            message=f"Blocked because dependencies did not succeed: {failed_deps}.",
                        ),
                    )
                    continue
                try:
                    ctx.budget.start_step()
                except BudgetExceeded as exc:
                    self._finish(state, StepStatus.FAILED, failure=self._failure(state, exc))
                    continue
                inputs = {dep: states[dep].artifact() for dep in state.step.depends_on}
                state.status = StepStatus.RUNNING
                task = asyncio.create_task(
                    self._run_step(state, ctx, task_factory(state.step, inputs), semaphore),
                    name=f"step:{state.step.id}",
                )
                running[task] = state.step.id

            if not running:
                break
            timeout = max(0.01, min(self.CANCEL_POLL_SECONDS, ctx.budget.remaining_seconds))
            done, _ = await asyncio.wait(set(running), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                running.pop(task, None)
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:  # defensive: _run_step handles its own errors
                    logger.error("step_task_crashed", extra={"step_id": task.get_name()}, exc_info=error)

        # Anything still pending could never become ready.
        for state in states.values():
            if not state.terminal:
                self._finish(
                    state,
                    StepStatus.CANCELLED if ctx.cancel_event.is_set() else StepStatus.BLOCKED,
                    failure=StructuredFailure(
                        agent=state.step.agent,
                        step_id=state.step.id,
                        status="blocked",
                        error_type=ErrorType.CANCELLED if ctx.cancel_event.is_set() else ErrorType.PERMANENT,
                        retryable=False,
                        message="Step did not run.",
                    ),
                )
        return states

    # ----------------------------------------------------------------- steps

    @staticmethod
    def _readiness(state: StepState, states: dict[str, StepState]) -> str:
        dependencies = [states[dep] for dep in state.step.depends_on]
        if any(not dep.terminal for dep in dependencies):
            return "wait"
        if not dependencies:
            return "ready"
        mode = state.step.dependency_mode
        if mode == "all_done":
            return "ready"
        succeeded = [dep for dep in dependencies if dep.status == StepStatus.SUCCESS]
        if mode == "any_success":
            return "ready" if succeeded else "blocked"
        return "ready" if len(succeeded) == len(dependencies) else "blocked"

    async def _run_step(
        self, state: StepState, ctx: RunContext, task: AgentTask, semaphore: asyncio.Semaphore
    ) -> None:
        step = state.step
        spec = self.registry.get(step.agent or "")
        policy = spec.retry_policy.model_copy(update={"max_retries": min(spec.retry_policy.max_retries, step.retry_limit)})
        async with semaphore:
            state.started_monotonic = time.monotonic()
            self.sink.step(step.id, status=StepStatus.RUNNING.value, started_at=utc_now())
            self.sink.emit(
                "node_started",
                node_id=step.id,
                node_type=step.node_type,
                label=_label(step.agent),
                status="running",
                details={"depends_on": step.depends_on, "agent": step.agent, "capability": step.capability},
            )
            while True:
                state.attempts += 1
                attempt_timeout = min(step.timeout_seconds or spec.timeout_seconds, ctx.budget.remaining_seconds)
                agent_ctx = AgentContext(run=ctx, spec=spec, step_id=step.id)
                attempt_started = time.perf_counter()
                try:
                    if attempt_timeout <= 0:
                        raise BudgetExceeded("Run exceeded its maximum runtime.", details={"limit": "max_runtime"})
                    async with ctx.telemetry.span(
                        "agent", spec.name, step_id=step.id, attempt=state.attempts, capability=step.capability
                    ) as span:
                        result = await asyncio.wait_for(spec.handler(task, agent_ctx), timeout=attempt_timeout)
                        if not isinstance(result, AgentResult):
                            raise ModelError(f"Agent {spec.name} returned {type(result).__name__}, expected AgentResult.", retryable=False)
                        if not result.summary.strip():
                            raise ModelError("Agent returned an empty summary.", retryable=True)
                        span.set(tokens=agent_ctx.tokens_used, retry_count=state.attempts - 1)
                    state.result = result
                    self._finish(state, StepStatus.SUCCESS, output=result.summary[:2_000], result=result)
                    return
                except asyncio.CancelledError:
                    # The canceller (_cancel_all) records CANCELLED or TIMEOUT.
                    raise
                except Exception as exc:  # classified below; never swallowed
                    if isinstance(exc, asyncio.TimeoutError):
                        exc = TimeoutError(f"Step exceeded its {attempt_timeout:.1f}s timeout.")
                    error_type, retryable = classify_exception(exc)
                    failure = self._failure(state, exc)
                    elapsed_ms = int((time.perf_counter() - attempt_started) * 1000)
                    will_retry = (
                        policy.should_retry(error_type, retryable, state.attempts)
                        and ctx.budget.remaining_seconds > 1
                        and not ctx.cancel_event.is_set()
                        and ctx.budget.use_retry()
                    )
                    self.sink.emit(
                        "node_failed",
                        node_id=step.id,
                        node_type=step.node_type,
                        label=_label(step.agent),
                        status="retrying" if will_retry else self._terminal_status(error_type, exc).value.lower(),
                        details={
                            "error": failure.model_dump(mode="json"),
                            "retry_count": state.attempts - 1,
                            "attempt_ms": elapsed_ms,
                            "depends_on": step.depends_on,
                        },
                    )
                    if not will_retry:
                        self._finish(state, self._terminal_status(error_type, exc), failure=failure)
                        return
                    delay = min(policy.delay(state.attempts), max(0.0, ctx.budget.remaining_seconds - 1))
                    state.status = StepStatus.RETRYING
                    self.sink.step(step.id, status=StepStatus.RETRYING.value, attempts=state.attempts)
                    self.sink.emit(
                        "node_retrying",
                        node_id=step.id,
                        node_type=step.node_type,
                        label=_label(step.agent),
                        status="retrying",
                        details={
                            "retry_count": state.attempts,
                            "backoff_seconds": round(delay, 3),
                            "error_type": error_type.value,
                            "depends_on": step.depends_on,
                        },
                    )
                    await asyncio.sleep(delay)

    @staticmethod
    def _terminal_status(error_type: ErrorType, exc: BaseException) -> StepStatus:
        if error_type == ErrorType.TIMEOUT:
            return StepStatus.TIMEOUT
        if error_type == ErrorType.SECURITY or isinstance(exc, HumanApprovalRequired):
            return StepStatus.BLOCKED
        if error_type == ErrorType.CANCELLED:
            return StepStatus.CANCELLED
        return StepStatus.FAILED

    def _failure(self, state: StepState, exc: BaseException) -> StructuredFailure:
        from app.monitoring.logging import redact

        error_type, retryable = classify_exception(exc)
        details: dict[str, Any] = dict(getattr(exc, "details", {}) or {})
        if isinstance(exc, HumanApprovalRequired):
            details["approval_required_for"] = exc.tool_name
        return StructuredFailure(
            agent=state.step.agent,
            step_id=state.step.id,
            status=self._terminal_status(error_type, exc).value.lower(),
            error_type=error_type,
            retryable=retryable,
            message=redact(f"{exc.__class__.__name__}: {exc}")[:500],
            attempts=state.attempts,
            details=details,
        )

    def _finish(
        self,
        state: StepState,
        status: StepStatus,
        *,
        failure: StructuredFailure | None = None,
        output: str | None = None,
        result: AgentResult | None = None,
    ) -> None:
        state.status = status
        state.failure = failure
        if state.started_monotonic is not None:
            state.latency_ms = int((time.monotonic() - state.started_monotonic) * 1000)
        step = state.step
        self.sink.step(
            step.id,
            status=status.value,
            attempts=state.attempts,
            error=failure.model_dump(mode="json") if failure else None,
            output_preview=(output or (failure.message if failure else ""))[:2_000],
            latency_ms=state.latency_ms,
            completed_at=utc_now(),
        )
        event_type = {
            StepStatus.SUCCESS: "node_completed",
            StepStatus.BLOCKED: "node_blocked",
            StepStatus.CANCELLED: "node_cancelled",
            StepStatus.TIMEOUT: "node_timeout",
        }.get(status, "node_failed")
        details: dict[str, Any] = {
            "depends_on": step.depends_on,
            "duration_ms": state.latency_ms,
            "retry_count": max(0, state.attempts - 1),
            "step_status": status.value,
        }
        if output is not None:
            details["output"] = output
        if result is not None:
            details["result"] = {
                "findings": result.findings[:20],
                "warnings": result.warnings[:20],
                "sources": [source.model_dump(mode="json") for source in result.sources[:20]],
                "artifacts": result.artifacts[:20],
            }
        if failure is not None:
            details["error"] = failure.model_dump(mode="json")
        self.sink.emit(
            event_type,
            node_id=step.id,
            node_type=step.node_type,
            label=_label(step.agent),
            status={
                StepStatus.SUCCESS: "completed",
                StepStatus.BLOCKED: "blocked",
                StepStatus.CANCELLED: "cancelled",
                StepStatus.TIMEOUT: "timeout",
            }.get(status, "failed"),
            details=details,
        )

    async def _cancel_all(
        self,
        states: dict[str, StepState],
        running: dict[asyncio.Task[None], str],
        status: StepStatus,
        message: str,
        error_type: ErrorType,
    ) -> None:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        running.clear()
        for state in states.values():
            if not state.terminal:
                self._finish(
                    state,
                    status,
                    failure=StructuredFailure(
                        agent=state.step.agent,
                        step_id=state.step.id,
                        status=status.value.lower(),
                        error_type=error_type,
                        retryable=False,
                        message=message,
                        attempts=state.attempts,
                    ),
                )
