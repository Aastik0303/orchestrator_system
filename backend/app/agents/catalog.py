"""Agent discovery: importing every module in `app.agents` registers its
agents. Dropping a new module with `@register_agent(...)` into this package is
all it takes to add an agent."""

from __future__ import annotations

import importlib
import pkgutil
import threading

import app.agents as agents_package
from app.agents.registry import AgentRegistry, agent_registry

_INFRASTRUCTURE_MODULES = {"registry", "context", "prompts", "common", "base", "catalog", "planner"}
_loaded = False
_lock = threading.Lock()


def load_agents() -> AgentRegistry:
    global _loaded
    if _loaded:
        return agent_registry
    with _lock:
        if not _loaded:
            for module in pkgutil.iter_modules(agents_package.__path__):
                if module.name not in _INFRASTRUCTURE_MODULES and not module.name.startswith("_"):
                    importlib.import_module(f"{agents_package.__name__}.{module.name}")
            _loaded = True
    return agent_registry
