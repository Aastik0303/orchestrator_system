"""Planner: decomposes a routed request into an execution DAG.

Example: "Analyze this CSV and research the market, then create a report"

    memory ──┬──> data_analysis ──┐
             └──> web_research ───┴──> report_synthesis ──> validation

* Capabilities mentioned in the same clause are independent (run in parallel).
* Sequencing markers ("then", "after that", "based on the results") make a
  clause depend on the previous clause.
* Synthesis waits for every task step and tolerates partial failure.
* Memory retrieval is optional context (`all_done`): its failure never blocks.
"""

from __future__ import annotations

import re

from app.agents.catalog import load_agents
from app.agents.registry import AgentRegistry
from app.guardrails.detectors import normalize
from app.models import AgentName, ChatRequest, ExecutionPlan, PlanStep, RoutingDecision
from app.orchestrator.capabilities import SYNTHESIS_CAPABILITIES
from app.orchestrator.planner_validator import validate_plan

SEQUENCE_SPLIT_RE = re.compile(
    r"(?:,|;)?\s*\b(?:and then|then|after that|afterwards|finally|once (?:that is|that's|it is) done|"
    r"based on (?:that|this|those|these|the results?|the findings)|using (?:the|those|these) (?:results|findings))\b",
    re.IGNORECASE,
)


def _clauses(message: str) -> list[str]:
    parts = [part.strip() for part in SEQUENCE_SPLIT_RE.split(normalize(message)) if part and part.strip()]
    return parts or [normalize(message)]


def _capability_clause_index(capabilities: list[str], clauses: list[str], router) -> dict[str, int]:
    """Assign each capability to the first clause whose text matches it."""
    catalog = router.catalog()
    index: dict[str, int] = {}
    for capability in capabilities:
        definition = catalog.get(capability)
        for position, clause in enumerate(clauses):
            if definition is not None and definition.score(clause)[0] > 0:
                index[capability] = position
                break
        index.setdefault(capability, 0)
    return index


def create_execution_plan(
    request: ChatRequest,
    route: RoutingDecision,
    registry: AgentRegistry | None = None,
) -> ExecutionPlan:
    from app.orchestrator.router import router

    registry = registry or load_agents()
    if not route.requires_planning:
        plan = ExecutionPlan(
            goal=request.message,
            steps=[
                PlanStep(
                    id=f"agent_{route.primary_agent}",
                    node_type="agent",
                    agent=route.primary_agent,
                    capability=route.required_capabilities[0] if route.required_capabilities else None,
                    description=f"Execute {route.primary_agent.replace('_', ' ')}.",
                    instruction=request.message,
                    retry_limit=registry.get(route.primary_agent).retry_policy.max_retries
                    if registry.has(route.primary_agent)
                    else 1,
                )
            ],
            strategy="single",
        )
        validate_plan(plan, registry=registry)
        return plan

    steps: list[PlanStep] = []
    memory_dependency: list[str] = []
    if registry.has(AgentName.MEMORY.value):
        steps.append(
            PlanStep(
                id="memory_1",
                node_type="agent",
                agent=AgentName.MEMORY.value,
                capability="memory_retrieval",
                description="Retrieve scoped long-term memory relevant to the request.",
            )
        )
        memory_dependency = ["memory_1"]

    # Pair each routed task capability with its agent.
    task_pairs: list[tuple[str, str]] = []
    agents_in_order = [route.primary_agent, *route.secondary_agents]
    for capability in route.required_capabilities:
        if capability in SYNTHESIS_CAPABILITIES:
            continue
        agent = next(
            (
                name
                for name in agents_in_order
                if registry.has(name) and capability in registry.get(name).capabilities
            ),
            None,
        )
        if agent is None:
            candidates = registry.find_by_capability(capability)
            agent = candidates[0].name if candidates else None
        if agent and (capability, agent) not in task_pairs:
            task_pairs.append((capability, agent))
    if not task_pairs:
        task_pairs = [(route.required_capabilities[0] if route.required_capabilities else "conversation", route.primary_agent)]

    clauses = _clauses(request.message)
    clause_of = _capability_clause_index([capability for capability, _ in task_pairs], clauses, router)
    by_clause: dict[int, list[str]] = {}
    task_ids: list[str] = []
    for index, (capability, agent) in enumerate(task_pairs, start=1):
        step_id = f"{agent}_{index}"
        clause_position = clause_of.get(capability, 0)
        previous_clause_steps: list[str] = []
        for position in range(clause_position - 1, -1, -1):
            if by_clause.get(position):
                previous_clause_steps = by_clause[position]
                break
        depends_on = memory_dependency + previous_clause_steps
        parallel_peers = sum(1 for cap, _ in task_pairs if clause_of.get(cap, 0) == clause_position)
        steps.append(
            PlanStep(
                id=step_id,
                node_type="agent",
                agent=agent,
                capability=capability,
                depends_on=depends_on,
                dependency_mode="all_done" if not previous_clause_steps else "all_success",
                description=f"Execute {agent.replace('_', ' ')} for {capability.replace('_', ' ')}.",
                instruction=clauses[clause_position] if clause_position < len(clauses) else request.message,
                parallel_group=f"clause_{clause_position}" if parallel_peers > 1 else None,
                retry_limit=registry.get(agent).retry_policy.max_retries,
            )
        )
        by_clause.setdefault(clause_position, []).append(step_id)
        task_ids.append(step_id)

    report_agents = registry.find_by_capability("report_synthesis")
    final_ids = task_ids
    if report_agents:
        steps.append(
            PlanStep(
                id="report_1",
                node_type="agent",
                agent=report_agents[0].name,
                capability="report_synthesis",
                depends_on=task_ids,
                dependency_mode="any_success",
                description="Combine structured findings into the final Markdown report.",
                retry_limit=0,
            )
        )
        final_ids = ["report_1"]
        validators = registry.find_by_capability("validation")
        if validators:
            steps.append(
                PlanStep(
                    id="evaluation_1",
                    node_type="agent",
                    agent=validators[0].name,
                    capability="validation",
                    depends_on=final_ids,
                    description="Evaluate relevance, completeness, groundedness, and format.",
                    retry_limit=0,
                )
            )
    plan = ExecutionPlan(goal=request.message, steps=steps, strategy="dag")
    validate_plan(plan, registry=registry)
    return plan
