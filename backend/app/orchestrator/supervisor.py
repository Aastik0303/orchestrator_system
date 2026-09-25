"""Backward-compatible view of the task agents.

Dispatch is driven by the Agent Registry (`app.agents.registry`); this mapping
is derived from it rather than hard-coded.
"""

from app.agents.catalog import load_agents


def task_agents() -> dict[str, object]:
    return {spec.name: spec.handler for spec in load_agents().all(category="task")}
