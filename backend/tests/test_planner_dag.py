import _env  # noqa: F401  (must be first)

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app.agents.catalog import load_agents
from app.agents.context import RunContext
from app.agents.registry import AgentRegistry, AgentSpec, RetryPolicy
from app.core.budget import Budget, ExecutionLimits
from app.core.errors import InvalidInputError, TransientError
from app.models import AgentResult, AgentTask, ChatRequest, ExecutionPlan, PlanStep, StepStatus
from app.observability.telemetry import Telemetry
from app.orchestrator.dag import DagExecutor
from app.orchestrator.events import EventSink
from app.orchestrator.planner import create_execution_plan
from app.orchestrator.planner_validator import validate_plan
from app.orchestrator.router import choose_route
from app.services.runtime_store import RuntimeStore


class PlannerTests(unittest.TestCase):
    def test_parallel_plan_is_valid_and_topologically_sorted(self):
        request = ChatRequest(message="Analyze this repository and research current architecture patterns")
        plan = create_execution_plan(request, choose_route(request))
        ordered = validate_plan(plan, registry=load_agents())
        self.assertEqual(ordered[0].id, "memory_1")
        self.assertEqual(ordered[-1].id, "evaluation_1")
        parallel = {step.agent for step in plan.steps if step.parallel_group}
        self.assertEqual(parallel, {"code_dev", "deep_research"})

    def test_csv_research_report_plan_shape(self):
        request = ChatRequest(message="Analyze this CSV and research the market, then create a report")
        plan = create_execution_plan(request, choose_route(request))
        steps = {step.id: step for step in plan.steps}
        data = next(step for step in plan.steps if step.agent == "data_analyst")
        research = next(step for step in plan.steps if step.agent == "deep_research")
        self.assertEqual(data.depends_on, ["memory_1"])
        self.assertEqual(research.depends_on, ["memory_1"])
        self.assertEqual(set(steps["report_1"].depends_on), {data.id, research.id})
        self.assertEqual(steps["report_1"].dependency_mode, "any_success")
        self.assertEqual(steps["evaluation_1"].depends_on, ["report_1"])

    def test_sequencing_markers_create_dependencies(self):
        request = ChatRequest(message="Research the vector database market, then write python code that ranks the options")
        plan = create_execution_plan(request, choose_route(request))
        research = next(step for step in plan.steps if step.agent == "deep_research")
        code = next(step for step in plan.steps if step.agent == "code_dev")
        self.assertIn(research.id, code.depends_on)

    def test_simple_request_is_a_single_step(self):
        request = ChatRequest(message="hello")
        plan = create_execution_plan(request, choose_route(request))
        self.assertEqual(len(plan.steps), 1)

    def test_validator_rejects_bad_plans(self):
        step = lambda **kw: PlanStep(node_type="agent", description="x", **kw)  # noqa: E731
        cases = {
            "cycle": [step(id="a", agent="code_dev", depends_on=["b"]), step(id="b", agent="code_dev", depends_on=["a"])],
            "missing dependencies": [step(id="a", agent="code_dev", depends_on=["zzz"])],
            "unknown agent": [step(id="a", agent="not_an_agent")],
            "duplicate": [step(id="a", agent="code_dev"), step(id="a", agent="code_dev")],
        }
        for message, steps in cases.items():
            with self.assertRaisesRegex(ValueError, message):
                validate_plan(ExecutionPlan(goal="x", steps=steps), registry=load_agents())
        too_many = [step(id=f"s{i}", agent="code_dev") for i in range(100)]
        with self.assertRaisesRegex(ValueError, "exceeds"):
            validate_plan(ExecutionPlan(goal="x", steps=too_many))


class DagTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.temp_dir.name) / "runtime.db")
        self.store.initialize()
        self.store.create_run(run_id="run_dag", user_id="u", project_id="p", session_id=None, task="t", file_count=0)
        self.registry = AgentRegistry()
        self.sink = EventSink(self.store, "run_dag").start()

    async def asyncTearDown(self):
        await self.sink.aclose()
        self.store.close()
        self.temp_dir.cleanup()

    def agent(self, name, handler, **kwargs):
        self.registry.register(
            AgentSpec(name=name, description=name, capabilities=[name], handler=handler, **kwargs), replace=True
        )

    def ctx(self, **limit_overrides) -> RunContext:
        limits = ExecutionLimits(
            max_steps=limit_overrides.get("max_steps", 20),
            max_runtime_seconds=limit_overrides.get("max_runtime_seconds", 30),
            max_tool_calls=limit_overrides.get("max_tool_calls", 10),
            max_tokens=limit_overrides.get("max_tokens", 10000),
            max_retries=limit_overrides.get("max_retries", 5),
            max_llm_calls=10,
            max_parallel_steps=limit_overrides.get("max_parallel_steps", 4),
        )
        return RunContext(
            run_id="run_dag",
            user_id="u",
            project_id="p",
            session_id=None,
            trace_id="t",
            budget=Budget(limits),
            telemetry=Telemetry(run_id="run_dag"),
            store=self.store,
        )

    async def execute(self, steps, ctx=None):
        ctx = ctx or self.ctx()
        executor = DagExecutor(self.registry, self.sink)

        def factory(step, inputs):
            return AgentTask(step_id=step.id, agent=step.agent, goal="goal", instruction="i", inputs=inputs)

        states = await executor.execute(ExecutionPlan(goal="g", steps=steps), ctx, factory)
        await self.sink.flush()
        return states

    @staticmethod
    def step(step_id, agent, depends_on=(), **kwargs):
        return PlanStep(id=step_id, node_type="agent", agent=agent, depends_on=list(depends_on), description=step_id, **kwargs)


class DagExecutionTests(DagTestCase):
    async def test_independent_steps_run_concurrently_and_dependents_wait(self):
        timeline = {}

        def sleeper(name):
            async def handler(task, ctx):
                timeline[name] = [time.perf_counter()]
                await asyncio.sleep(0.3)
                timeline[name].append(time.perf_counter())
                return AgentResult(summary=f"{name} done")

            return handler

        async def synthesizer(task, ctx):
            timeline["synth"] = [time.perf_counter()]
            return AgentResult(summary="combined: " + ", ".join(sorted(task.inputs)))

        self.agent("research", sleeper("research"))
        self.agent("data", sleeper("data"))
        self.agent("synth", synthesizer)
        started = time.perf_counter()
        states = await self.execute(
            [self.step("r", "research"), self.step("d", "data"), self.step("s", "synth", ["r", "d"])]
        )
        elapsed = time.perf_counter() - started
        self.assertTrue(all(state.status == StepStatus.SUCCESS for state in states.values()))
        self.assertLess(elapsed, 0.55, f"parallel steps took {elapsed:.2f}s (sequential would be >= 0.6s)")
        self.assertGreaterEqual(timeline["synth"][0], max(timeline["research"][1], timeline["data"][1]))
        self.assertEqual(states["s"].result.summary, "combined: d, r")

    async def test_transient_errors_are_retried_with_backoff(self):
        calls = {"n": 0}

        async def flaky(task, ctx):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError("temporary provider failure")
            return AgentResult(summary="recovered")

        self.agent("flaky", flaky, retry_policy=RetryPolicy(max_retries=3, backoff_base_seconds=0.01))
        states = await self.execute([self.step("f", "flaky", retry_limit=3)])
        self.assertEqual(states["f"].status, StepStatus.SUCCESS)
        self.assertEqual(states["f"].attempts, 3)
        steps = self.store.list_steps("run_dag")
        self.assertEqual(steps[0]["status"], "SUCCESS")
        self.assertIn("node_retrying", [event["type"] for event in self.store.list_events("run_dag")])

    async def test_permanent_errors_are_not_retried(self):
        calls = {"n": 0}

        async def broken(task, ctx):
            calls["n"] += 1
            raise InvalidInputError("bad input")

        self.agent("broken", broken, retry_policy=RetryPolicy(max_retries=3))
        states = await self.execute([self.step("b", "broken", retry_limit=3)])
        self.assertEqual(calls["n"], 1)
        failure = states["b"].failure
        self.assertEqual((states["b"].status, failure.error_type.value, failure.retryable), (StepStatus.FAILED, "INVALID_INPUT", False))
        persisted = self.store.list_steps("run_dag")[0]
        self.assertEqual(persisted["error"]["error_type"], "INVALID_INPUT")

    async def test_empty_output_is_retried(self):
        calls = {"n": 0}

        async def empty_then_ok(task, ctx):
            calls["n"] += 1
            return AgentResult(summary="" if calls["n"] == 1 else "Recovered")

        self.agent("e", empty_then_ok, retry_policy=RetryPolicy(max_retries=1, backoff_base_seconds=0.01))
        states = await self.execute([self.step("e", "e")])
        self.assertEqual((states["e"].status, calls["n"]), (StepStatus.SUCCESS, 2))

    async def test_timeout_marks_step_and_blocks_dependents(self):
        async def slow(task, ctx):
            await asyncio.sleep(5)
            return AgentResult(summary="late")

        async def after(task, ctx):
            return AgentResult(summary="never")

        self.agent("slow", slow, timeout_seconds=0.2, retry_policy=RetryPolicy(max_retries=0))
        self.agent("after", after)
        states = await self.execute([self.step("s", "slow"), self.step("a", "after", ["s"])])
        self.assertEqual(states["s"].status, StepStatus.TIMEOUT)
        self.assertEqual(states["s"].failure.error_type.value, "TIMEOUT")
        self.assertTrue(states["s"].failure.retryable)
        self.assertEqual(states["a"].status, StepStatus.BLOCKED)

    async def test_any_success_dependency_runs_with_partial_results(self):
        async def ok(task, ctx):
            return AgentResult(summary="ok")

        async def fail(task, ctx):
            raise InvalidInputError("nope")

        async def synth(task, ctx):
            return AgentResult(summary="statuses " + ",".join(f"{k}={v.status}" for k, v in sorted(task.inputs.items())))

        self.agent("ok", ok)
        self.agent("fail", fail)
        self.agent("synth", synth)
        states = await self.execute(
            [self.step("o", "ok"), self.step("f", "fail"), self.step("s", "synth", ["o", "f"], dependency_mode="any_success")]
        )
        self.assertEqual(states["s"].status, StepStatus.SUCCESS)
        self.assertIn("f=FAILED", states["s"].result.summary)

    async def test_max_steps_budget_stops_safely(self):
        async def ok(task, ctx):
            return AgentResult(summary="ok")

        self.agent("ok", ok)
        states = await self.execute([self.step(f"s{i}", "ok") for i in range(4)], ctx=self.ctx(max_steps=2))
        statuses = sorted(state.status.value for state in states.values())
        self.assertEqual(statuses.count("SUCCESS"), 2)
        self.assertEqual(statuses.count("FAILED"), 2)
        failed = [state for state in states.values() if state.status == StepStatus.FAILED]
        self.assertEqual(failed[0].failure.error_type.value, "BUDGET_EXCEEDED")

    async def test_max_runtime_budget_times_out_running_steps(self):
        async def slow(task, ctx):
            await asyncio.sleep(5)
            return AgentResult(summary="late")

        self.agent("slow", slow, timeout_seconds=60)
        started = time.perf_counter()
        states = await self.execute([self.step("s", "slow")], ctx=self.ctx(max_runtime_seconds=0.4))
        self.assertLess(time.perf_counter() - started, 2)
        self.assertEqual(states["s"].status, StepStatus.TIMEOUT)

    async def test_tool_call_budget_is_enforced(self):
        async def greedy(task, ctx):
            for _ in range(5):
                ctx.run.budget.use_tool_call()
            return AgentResult(summary="too many")

        self.agent("greedy", greedy, retry_policy=RetryPolicy(max_retries=0))
        states = await self.execute([self.step("g", "greedy")], ctx=self.ctx(max_tool_calls=2))
        self.assertEqual(states["g"].failure.error_type.value, "BUDGET_EXCEEDED")

    async def test_run_wide_retry_budget_prevents_retry_storms(self):
        async def always_transient(task, ctx):
            raise TransientError("down")

        self.agent("t", always_transient, retry_policy=RetryPolicy(max_retries=5, backoff_base_seconds=0.001))
        ctx = self.ctx(max_retries=2)
        states = await self.execute([self.step("a", "t", retry_limit=5), self.step("b", "t", retry_limit=5)], ctx=ctx)
        total_attempts = sum(state.attempts for state in states.values())
        self.assertEqual(total_attempts, 4, "2 first attempts + 2 retries from the shared budget")
        self.assertEqual(ctx.budget.retries, 2)

    async def test_cancellation_stops_running_and_pending_steps(self):
        async def slow(task, ctx):
            await asyncio.sleep(5)
            return AgentResult(summary="late")

        self.agent("slow", slow, timeout_seconds=60)
        ctx = self.ctx()
        asyncio.get_running_loop().call_later(0.2, ctx.cancel_event.set)
        started = time.perf_counter()
        states = await self.execute([self.step("a", "slow"), self.step("b", "slow", ["a"])], ctx=ctx)
        self.assertLess(time.perf_counter() - started, 2)
        self.assertEqual(states["a"].status, StepStatus.CANCELLED)
        self.assertEqual(states["b"].status, StepStatus.CANCELLED)

    async def test_parallelism_is_bounded(self):
        active = {"now": 0, "max": 0}

        async def counted(task, ctx):
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return AgentResult(summary="ok")

        self.agent("c", counted)
        await self.execute([self.step(f"s{i}", "c") for i in range(8)], ctx=self.ctx(max_parallel_steps=3))
        self.assertLessEqual(active["max"], 3)


if __name__ == "__main__":
    unittest.main()
