from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING

from app.config import get_settings
from app.models import ExecutionPlan, PlanStep

if TYPE_CHECKING:
    from app.agents.registry import AgentRegistry


def validate_plan(
    plan: ExecutionPlan,
    *,
    valid_tools: set[str] | None = None,
    registry: "AgentRegistry | None" = None,
) -> list[PlanStep]:
    """Validate structure and limits; returns the steps in topological order."""
    settings = get_settings()
    if not plan.steps:
        raise ValueError("Execution plan must contain at least one step.")
    if len(plan.steps) > settings.max_plan_nodes:
        raise ValueError(f"Execution plan exceeds {settings.max_plan_nodes} nodes.")

    step_ids = [step.id for step in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("Execution plan contains duplicate step IDs.")
    known_ids = set(step_ids)
    for step in plan.steps:
        missing = set(step.depends_on) - known_ids
        if missing:
            raise ValueError(f"Step {step.id} has missing dependencies: {sorted(missing)}")
        if step.id in step.depends_on:
            raise ValueError(f"Step {step.id} cannot depend on itself.")
        if step.node_type == "agent":
            if step.agent is None:
                raise ValueError(f"Agent step {step.id} does not identify an agent.")
            if registry is not None and not registry.has(step.agent):
                raise ValueError(f"Step {step.id} references unknown agent {step.agent}.")
        if step.node_type == "tool":
            if not step.tool:
                raise ValueError(f"Tool step {step.id} does not identify a tool.")
            if valid_tools is not None and step.tool not in valid_tools:
                raise ValueError(f"Step {step.id} references unknown tool {step.tool}.")

    ordered = topological_sort(plan.steps)
    depths: dict[str, int] = {}
    for step in ordered:
        depths[step.id] = 1 + max((depths[dependency] for dependency in step.depends_on), default=0)
    if max(depths.values(), default=0) > settings.max_plan_depth:
        raise ValueError(f"Execution plan exceeds maximum depth {settings.max_plan_depth}.")
    return ordered


def topological_sort(steps: list[PlanStep]) -> list[PlanStep]:
    by_id = {step.id: step for step in steps}
    indegree = {step.id: len(step.depends_on) for step in steps}
    children: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for dependency in step.depends_on:
            children[dependency].append(step.id)
    queue = deque(step_id for step_id, degree in indegree.items() if degree == 0)
    ordered: list[PlanStep] = []
    while queue:
        step_id = queue.popleft()
        ordered.append(by_id[step_id])
        for child in children[step_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(steps):
        raise ValueError("Execution plan contains a dependency cycle.")
    return ordered
