"""Workflow orchestrator.

User -> Input Guardrail -> Intent Router -> Planner -> Execution DAG
     -> Agents + Tools (via Agent/Tool Registries) -> Result Aggregator
     -> Critic/Validator -> Output Guardrail -> Final Response

Each run gets its own RunContext (budget, telemetry, cancellation); nothing
about a run lives in module-level mutable state, so any API replica or worker
can execute any run. Durable state (runs, steps, events, spans, reports,
evaluations) is written through the repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.agents.catalog import load_agents
from app.agents.context import RunContext
from app.agents.registry import AgentRegistry
from app.agents.visualization import charts_from_metadata, render_chart_blocks, strip_chart_blocks
from app.config import get_settings
from app.core.budget import Budget, ExecutionLimits
from app.core.errors import ErrorType, StructuredFailure
from app.guardrails.input_guard import inspect_input
from app.guardrails.output_guard import inspect_output
from app.memory.service import save_workflow_memory
from app.models import (
    AgentName,
    AgentResponse,
    AgentResult,
    AgentTask,
    ChatRequest,
    EvaluationResult,
    ExecutionPlan,
    GuardrailFinding,
    GuardrailVerdict,
    PlanStep,
    RoutingDecision,
    StepArtifact,
    StepStatus,
)
from app.monitoring.logging import redact
from app.observability.telemetry import Telemetry, bind_run, current_trace_id, new_trace_id
from app.orchestrator.dag import DagExecutor, StepState
from app.orchestrator.events import EventSink
from app.orchestrator.planner import create_execution_plan
from app.orchestrator.router import HybridRouter
from app.orchestrator.router import router as default_router
from app.services.runtime_store import RuntimeStore, runtime_store, utc_now

logger = logging.getLogger("orchestrator.workflow")

NON_TASK_AGENTS = {AgentName.MEMORY.value, AgentName.REPORT_GENERATOR.value, AgentName.EVALUATION.value}
OFF_TOPIC_MESSAGE = (
    "This assistant is configured for specialist tasks only (data analysis, research, documents, "
    "code and execution). Please describe a task in one of those areas."
)
INTERNAL_ERROR_MESSAGE = "The workflow failed because of an internal error. The run events contain the recorded failure."


class DynamicOrchestrator:
    def __init__(
        self,
        store: RuntimeStore = runtime_store,
        *,
        registry: AgentRegistry | None = None,
        router: HybridRouter | None = None,
    ) -> None:
        self.store = store
        self._registry = registry
        self._router = router

    @property
    def registry(self) -> AgentRegistry:
        return self._registry or load_agents()

    @property
    def router(self) -> HybridRouter:
        return self._router or default_router

    async def run(
        self,
        request: ChatRequest,
        *,
        run_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        watch_cancellation: bool = False,
    ) -> AgentResponse:
        settings = get_settings()
        resolved_run_id = run_id or f"run_{uuid4().hex[:12]}"
        telemetry = Telemetry(run_id=resolved_run_id, trace_id=current_trace_id.get() or new_trace_id())
        started = perf_counter()
        with bind_run(telemetry, run_id=resolved_run_id, session_id=request.session_id):
            terminal_status = await self._ensure_run(request, resolved_run_id, telemetry.trace_id)
            if terminal_status is not None:
                # e.g. cancelled while queued: never execute it.
                return AgentResponse(
                    active_agent=AgentName.SUPERVISOR.value,
                    response=f"The run was not executed because it is already {terminal_status}.",
                    status=terminal_status,
                    run_id=resolved_run_id,
                    trace_id=telemetry.trace_id,
                )
            ctx = RunContext(
                run_id=resolved_run_id,
                user_id=request.user_id,
                project_id=request.project_id,
                session_id=request.session_id,
                trace_id=telemetry.trace_id,
                budget=Budget(ExecutionLimits.from_settings(settings)),
                telemetry=telemetry,
                approved_tools=frozenset(request.approved_tools),
                store=self.store,
                cancel_event=cancel_event or asyncio.Event(),
            )
            sink = EventSink(self.store, resolved_run_id).start()
            watcher = asyncio.create_task(self._watch_cancellation(ctx)) if watch_cancellation else None
            try:
                async with telemetry.span("api", "workflow"):
                    async with asyncio.timeout(ctx.budget.limits.max_runtime_seconds + 15):
                        response = await self._pipeline(request, ctx, sink, started)
            except Exception as exc:  # unexpected internal failure: recorded, never leaked
                logger.exception("workflow_crashed")
                failure = StructuredFailure(
                    status="failed",
                    error_type=ErrorType.PERMANENT,
                    retryable=False,
                    message=redact(f"{exc.__class__.__name__}: {exc}")[:500],
                )
                response = await self._finalize(
                    request,
                    ctx,
                    sink,
                    started,
                    status="failed",
                    text=INTERNAL_ERROR_MESSAGE,
                    route=None,
                    failures=[failure.model_dump(mode="json")],
                )
            finally:
                if watcher is not None:
                    watcher.cancel()
                await sink.aclose()
                await self._persist_trace(ctx)
            return response

    # ------------------------------------------------------------ pipeline

    async def _pipeline(
        self, request: ChatRequest, ctx: RunContext, sink: EventSink, started: float
    ) -> AgentResponse:
        settings = get_settings()
        telemetry = ctx.telemetry
        sink.emit(
            "workflow_started",
            node_id="input",
            node_type="input",
            label="User Request",
            status="completed",
            details={
                "input": request.message[:2_000],
                "file_count": len(request.files),
                "trace_id": ctx.trace_id,
                "depends_on": [],
            },
        )

        # 1. Input guardrail -------------------------------------------------
        async with telemetry.span("guardrail", "input") as span:
            verdict = inspect_input(request)
            span.set(action=verdict.action, categories=[finding.category for finding in verdict.findings])
        ctx.guardrail_verdicts.append(verdict)
        sink.emit(
            "guardrail_checked",
            node_id="input_guardrail",
            parent_node_id="input",
            node_type="logic",
            label="Input Guardrail",
            status="blocked" if verdict.blocked else "completed",
            details={"depends_on": ["input"], "verdict": verdict.model_dump(mode="json")},
        )
        if verdict.blocked:
            return await self._finalize(
                request, ctx, sink, started, status="blocked", text=verdict.message or "Request blocked.", route=None
            )

        # 2. Routing -----------------------------------------------------------
        sink.emit(
            "node_started",
            node_id="supervisor",
            node_type="logic",
            label="Intent Router",
            status="running",
            details={"depends_on": ["input_guardrail"]},
        )
        try:
            async with telemetry.span("routing", "hybrid_router") as span:
                route = await self.router.route(request, budget=ctx.budget)
                span.set(strategy=route.strategy, intent=route.intent, confidence=route.confidence)
        except ValueError as exc:
            failure = StructuredFailure(
                agent=AgentName.SUPERVISOR.value,
                status="failed",
                error_type=ErrorType.INVALID_INPUT,
                retryable=False,
                message=str(exc)[:500],
            )
            return await self._finalize(
                request, ctx, sink, started, status="failed", text=str(exc), route=None,
                failures=[failure.model_dump(mode="json")],
            )
        sink.update_run(active_agent=route.primary_agent, route_json=json.dumps(route.model_dump(mode="json")))
        sink.emit(
            "routing_completed",
            node_id="supervisor",
            node_type="logic",
            label="Intent Router",
            status="completed",
            details={"depends_on": ["input_guardrail"], "output": route.reason, "route": route.model_dump(mode="json")},
        )
        if route.required_capabilities == ["conversation"] and not settings.general_chat_enabled:
            verdict = GuardrailVerdict(
                stage="input",
                action="block",
                findings=[GuardrailFinding(category="off_topic", severity="medium", detail="No specialist capability matched.")],
                message=OFF_TOPIC_MESSAGE,
            )
            ctx.guardrail_verdicts.append(verdict)
            return await self._finalize(request, ctx, sink, started, status="blocked", text=OFF_TOPIC_MESSAGE, route=route)

        # 3. Planning ----------------------------------------------------------
        async with telemetry.span("planning", "planner") as span:
            plan = create_execution_plan(request, route, self.registry)
            span.set(steps=len(plan.steps), strategy=plan.strategy)
        sink.update_run(plan_json=json.dumps(plan.model_dump(mode="json")))
        if route.requires_planning:
            sink.emit(
                "plan_created",
                node_id="planner",
                node_type="agent",
                label="Planner",
                status="completed",
                details={
                    "depends_on": ["supervisor"],
                    "output": f"Validated {len(plan.steps)}-step execution plan.",
                    "plan": plan.model_dump(mode="json"),
                },
            )
        self._announce_plan(sink, plan, route.requires_planning)

        # 4. Execution DAG -----------------------------------------------------
        conversation = await self._conversation(request)
        executor = DagExecutor(self.registry, sink)
        states = await executor.execute(plan, ctx, lambda step, inputs: self._task(request, step, inputs, conversation))

        # 5. Aggregate + critic ------------------------------------------------
        async with telemetry.span("synthesis", "aggregate"):
            text, status, failures, evaluation, artifacts = self._aggregate(plan, states, ctx)
        return await self._finalize(
            request,
            ctx,
            sink,
            started,
            status=status,
            text=text,
            route=route,
            failures=failures,
            evaluation=evaluation,
            artifacts=artifacts,
        )

    def _task(
        self,
        request: ChatRequest,
        step: PlanStep,
        inputs: dict[str, StepArtifact],
        conversation: list[dict[str, str]],
    ) -> AgentTask:
        memory: list[dict[str, Any]] = []
        task_inputs: dict[str, StepArtifact] = {}
        for step_id, artifact in inputs.items():
            if artifact.agent == AgentName.MEMORY.value:
                if artifact.status == StepStatus.SUCCESS.value:
                    memory = list(artifact.metadata.get("memories", []))
            else:
                task_inputs[step_id] = artifact
        is_task_agent = step.agent not in NON_TASK_AGENTS
        return AgentTask(
            step_id=step.id,
            agent=step.agent or "",
            capability=step.capability,
            goal=request.message,
            instruction=step.instruction or step.description,
            files=request.files or request.session_files,
            inputs=task_inputs,
            memory=memory if is_task_agent else [],
            conversation=conversation if is_task_agent else [],
            user_id=request.user_id,
            project_id=request.project_id,
            session_id=request.session_id,
            deep_research=request.deep_research,
        )

    def _aggregate(
        self, plan: ExecutionPlan, states: dict[str, StepState], ctx: RunContext
    ) -> tuple[str, str, list[dict[str, Any]], EvaluationResult | None, list[dict[str, Any]]]:
        task_states = [state for state in states.values() if state.step.agent not in NON_TASK_AGENTS]
        failures = [
            state.failure.model_dump(mode="json")
            for state in states.values()
            if state.failure is not None and state.status != StepStatus.SUCCESS
        ]
        succeeded = [state for state in task_states if state.status == StepStatus.SUCCESS]
        artifacts = [artifact for state in succeeded for artifact in (state.result.artifacts if state.result else [])]
        evaluation = None
        evaluation_state = next((state for state in states.values() if state.step.agent == AgentName.EVALUATION.value), None)
        if evaluation_state and evaluation_state.status == StepStatus.SUCCESS and evaluation_state.result:
            payload = evaluation_state.result.metadata.get("evaluation")
            evaluation = EvaluationResult.model_validate(payload) if payload else None

        if ctx.cancel_event.is_set():
            return "The run was cancelled before it finished.", "cancelled", failures, evaluation, artifacts

        report_state = next((state for state in states.values() if state.step.agent == AgentName.REPORT_GENERATOR.value), None)
        if report_state and report_state.status == StepStatus.SUCCESS and report_state.result:
            text = report_state.result.summary
        elif len(succeeded) == 1:
            text = _render_direct_result(succeeded[0].result)
        elif succeeded:
            from app.agents.report_generator import generate_report

            text = generate_report(None, {state.step.id: state.result for state in succeeded if state.result})
        else:
            text = ""

        if succeeded:
            return text, "completed", failures, evaluation, artifacts

        task_statuses = {state.status for state in task_states}
        if task_statuses and task_statuses <= {StepStatus.BLOCKED}:
            status = "blocked"
        elif StepStatus.TIMEOUT in task_statuses:
            status = "timeout"
        else:
            status = "failed"
        lines = ["The workflow could not complete:"]
        for state in task_states:
            if state.failure:
                detail = state.failure.message
                approval = state.failure.details.get("approval_required_for")
                if approval:
                    detail = f"requires your approval to use `{approval}` (resend with approved_tools including it)"
                lines.append(f"- {_label(state.step.agent)}: {state.status.value} ({state.failure.error_type.value}) - {detail}")
        return "\n".join(lines), status, failures, evaluation, artifacts

    async def _finalize(
        self,
        request: ChatRequest,
        ctx: RunContext,
        sink: EventSink,
        started: float,
        *,
        status: str,
        text: str,
        route: RoutingDecision | None,
        failures: list[dict[str, Any]] | None = None,
        evaluation: EvaluationResult | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        failures = failures or []
        artifacts = artifacts or []
        # 6. Output guardrail ------------------------------------------------
        async with ctx.telemetry.span("guardrail", "output") as span:
            safe_text, output_verdict = inspect_output(text)
            span.set(action=output_verdict.action)
        if output_verdict.findings:
            ctx.guardrail_verdicts.append(output_verdict)
            sink.emit(
                "guardrail_checked",
                node_id="output_guardrail",
                node_type="logic",
                label="Output Guardrail",
                status="blocked" if output_verdict.blocked else "completed",
                details={"verdict": output_verdict.model_dump(mode="json"), "depends_on": []},
            )

        duration_ms = int((perf_counter() - started) * 1000)
        metrics = {**ctx.budget.snapshot(), **ctx.telemetry.summary(), "duration_ms": duration_ms}
        guardrails = [verdict.model_dump(mode="json") for verdict in ctx.guardrail_verdicts if verdict.findings]

        event_type = {
            "completed": "workflow_completed",
            "cancelled": "workflow_cancelled",
            "blocked": "workflow_blocked",
            "timeout": "workflow_timeout",
        }.get(status, "workflow_failed")
        sink.emit(
            event_type,
            node_id="final",
            node_type="output",
            label="Final Response",
            status={"completed": "completed", "cancelled": "cancelled", "blocked": "blocked"}.get(status, "failed"),
            details={
                "depends_on": [],
                "output": safe_text,
                "duration_ms": duration_ms,
                "evaluation_score": evaluation.overall_score if evaluation else None,
                "metrics": {key: metrics[key] for key in ("tokens", "llm_calls", "tool_calls", "retries", "steps")},
                "failures": failures,
            },
        )
        # Events are persisted BEFORE the terminal status so stream readers
        # that observe the terminal status have every event available.
        await sink.flush()
        # Report, evaluation and terminal status: one transaction.
        updated = await asyncio.to_thread(
            self.store.complete_run,
            ctx.run_id,
            status=status,
            report={
                "user_id": request.user_id,
                "project_id": request.project_id,
                "title": request.message[:100],
                "content": safe_text,
            }
            if status == "completed"
            else None,
            evaluation={
                "user_id": request.user_id,
                "project_id": request.project_id,
                "result": evaluation.model_dump(mode="json"),
            }
            if evaluation is not None
            else None,
            response=safe_text,
            artifacts_json=json.dumps(artifacts, default=str),
            needs_clarification=0,
            error=failures[0]["message"] if failures and status != "completed" else None,
            error_json=json.dumps(failures, default=str) if failures else None,
            metrics_json=json.dumps(metrics, default=str),
            guardrails_json=json.dumps(guardrails, default=str) if guardrails else None,
            completed_at=utc_now(),
            duration_ms=duration_ms,
        )
        if not updated:
            # A cancellation (possibly from another replica) won the race.
            persisted_status, _ = await asyncio.to_thread(self.store.get_run_status, ctx.run_id)
            if persisted_status and persisted_status != status:
                status = persisted_status
                sink.emit(
                    "workflow_cancelled" if status == "cancelled" else f"workflow_{status}",
                    node_id="final",
                    node_type="output",
                    label="Final Response",
                    status=status,
                    details={"depends_on": [], "note": "Terminal status was set by another process."},
                )
        if status == "completed" and route is not None:
            # Best-effort memory runs after the response, off the critical path.
            self._spawn_background(self._remember(request, ctx, route))
        return AgentResponse(
            active_agent=route.primary_agent if route else AgentName.SUPERVISOR.value,
            response=safe_text,
            status=status,
            artifacts=artifacts,
            route=route,
            evaluation=evaluation,
            failures=failures,
            guardrails=[verdict for verdict in ctx.guardrail_verdicts if verdict.findings],
            metrics=metrics,
            run_id=ctx.run_id,
            trace_id=ctx.trace_id,
        )

    # ------------------------------------------------------------- helpers

    async def _ensure_run(self, request: ChatRequest, run_id: str, trace_id: str) -> str | None:
        return await asyncio.to_thread(
            self.store.start_run,
            run_id=run_id,
            user_id=request.user_id,
            project_id=request.project_id,
            session_id=request.session_id,
            task=request.message,
            file_count=len(request.files),
            trace_id=trace_id,
        )

    async def _conversation(self, request: ChatRequest) -> list[dict[str, str]]:
        if not request.session_id:
            return []
        try:
            messages = await asyncio.to_thread(
                self.store.list_chat_messages, request.session_id, user_id=request.user_id, limit=7
            )
        except Exception:
            logger.warning("conversation_load_failed")
            return []
        turns = [{"role": item["role"], "content": strip_chart_blocks(item["content"])} for item in messages]
        if turns and turns[-1]["role"] == "user" and turns[-1]["content"] == request.message:
            turns = turns[:-1]
        return turns[-6:]

    def _spawn_background(self, coroutine) -> None:
        tasks = self.__dict__.setdefault("_background_tasks", set())
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def drain_background(self, timeout: float = 30) -> None:
        """Wait for best-effort background work (memory saves); called on
        shutdown and by tests."""
        tasks = list(self.__dict__.get("_background_tasks", ()))
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def _remember(self, request: ChatRequest, ctx: RunContext, route: RoutingDecision) -> None:
        """Best-effort long-term memory; never fails the run. Bounded so a
        burst of completions cannot saturate the CPU with embedding calls."""
        slots = self.__dict__.setdefault("_memory_slots", weakref.WeakKeyDictionary())
        semaphore = slots.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(2))
        try:
            async with semaphore:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        save_workflow_memory,
                        f"Request: {request.message}\nSuccessful route: {route.primary_agent}",
                        user_id=request.user_id,
                        project_id=request.project_id,
                        run_id=ctx.run_id,
                        session_id=request.session_id,
                        store=self.store,
                        request=request.message,
                    ),
                    timeout=10,
                )
        except Exception as exc:
            logger.warning("workflow_memory_skipped", extra={"error_type": exc.__class__.__name__})

    async def _watch_cancellation(self, ctx: RunContext) -> None:
        """Poll the durable cancel flag so any replica can cancel this run."""
        while not ctx.cancel_event.is_set():
            await asyncio.sleep(DagExecutor.CANCEL_POLL_SECONDS)
            try:
                status, cancel_requested = await asyncio.to_thread(self.store.get_run_status, ctx.run_id)
            except Exception:
                continue
            if cancel_requested or status == "cancelled":
                ctx.cancel_event.set()

    async def _persist_trace(self, ctx: RunContext) -> None:
        try:
            spans = [span.to_dict() for span in ctx.telemetry.spans]
            await asyncio.to_thread(self.store.save_spans, ctx.run_id, spans)
        except Exception:
            logger.exception("trace_persist_failed")

    def _announce_plan(self, sink: EventSink, plan: ExecutionPlan, planned: bool) -> None:
        parent_fallback = "planner" if planned else "supervisor"
        for step in plan.steps:
            sink.emit(
                "node_added",
                node_id=step.id,
                parent_node_id=step.depends_on[0] if step.depends_on else parent_fallback,
                node_type=step.node_type,
                label=_label(step.agent) if step.agent else step.tool or step.id,
                status="queued",
                details={
                    "depends_on": step.depends_on or [parent_fallback],
                    "description": step.description,
                    "parallel_group": step.parallel_group,
                    "retry_limit": step.retry_limit,
                    "agent": step.agent,
                    "capability": step.capability,
                    "dependency_mode": step.dependency_mode,
                    "tool": step.tool,
                },
            )


def _label(agent: str | None) -> str:
    return agent.replace("_", " ").title() if agent else "Workflow Node"


def _render_direct_result(result: AgentResult | None) -> str:
    if result is None:
        return ""
    sections = [result.summary.strip()]
    if result.findings:
        sections.append("\n".join(f"- {item}" for item in result.findings))
    if result.recommendations:
        sections.append("\n".join(f"{index}. {item}" for index, item in enumerate(result.recommendations, 1)))
    if result.warnings:
        sections.append("\n".join(f"> {warning}" for warning in result.warnings))
    sections.append(render_chart_blocks(charts_from_metadata(result.metadata)))
    return "\n\n".join(section for section in sections if section).strip()


orchestrator = DynamicOrchestrator()
