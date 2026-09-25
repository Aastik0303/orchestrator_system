"""Helpers shared by agent handlers."""

from __future__ import annotations

from app.agents.prompts import wrap_untrusted
from app.config import get_settings
from app.models import AgentResult, AgentTask


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rsplit(" ", 1)[0] + "..."


def build_user_prompt(task: AgentTask, *, include_conversation: bool = True) -> str:
    """Render the task, bounded upstream artifacts, memory and recent
    conversation. Everything except the user's own request is delimited as
    untrusted data."""
    settings = get_settings()
    budget = settings.step_input_max_chars
    sections = [f"User request:\n{_clip(task.goal, budget)}"]
    if task.instruction and task.instruction != task.goal:
        sections.append(f"Your step in the plan:\n{_clip(task.instruction, 600)}")
    per_input = max(400, budget // max(1, len(task.inputs))) if task.inputs else 0
    for step_id, artifact in task.inputs.items():
        body = [artifact.summary]
        body.extend(f"- {finding}" for finding in artifact.findings[:8])
        sections.append(
            wrap_untrusted(
                "upstream_result",
                _clip("\n".join(body), per_input),
                step=step_id,
                agent=artifact.agent,
                status=artifact.status,
            )
        )
    if task.memory:
        memory_text = "\n".join(f"- {item.get('content', '')}" for item in task.memory[:4] if item.get("content"))
        if memory_text:
            sections.append(wrap_untrusted("memory", _clip(memory_text, 1200)))
    if include_conversation and task.conversation:
        turns = "\n".join(
            f"{turn.get('role', 'user')}: {_clip(turn.get('content', ''), 400)}" for turn in task.conversation[-6:]
        )
        sections.append(wrap_untrusted("conversation", _clip(turns, 2000)))
    return "\n\n".join(sections)


def provider_unavailable(agent_label: str) -> AgentResult:
    return AgentResult(
        summary=f"{agent_label} could not generate an answer because no model provider is configured.",
        warnings=["Set LLM_PROVIDER=groq and GROQ_API_KEY to enable model responses."],
        metadata={"generation_mode": "unavailable"},
    )
