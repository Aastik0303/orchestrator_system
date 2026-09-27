"""Helpers shared by agent handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents.prompts import wrap_untrusted
from app.config import get_settings
from app.models import AgentResult, AgentTask

if TYPE_CHECKING:
    from app.agents.context import AgentContext


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


def render_evidence(
    items: list[dict[str, Any]], ctx: "AgentContext", *, max_chars: int = 9000
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Guard and render externally fetched evidence (web pages, repository
    files, transcripts) exactly like retrieved documents: instruction-bearing
    items are quarantined, secrets/PII redacted, and everything is wrapped in
    numbered, untrusted <document> blocks the model can cite as [Source N].

    `items` carry `title`, `url` and `content`. Returns the rendered context,
    the kept items (same numbering) and warnings."""
    from app.guardrails.retrieval_guard import sanitize_retrieved_chunks

    safe, verdict = sanitize_retrieved_chunks([dict(item) for item in items if item.get("content", "").strip()])
    warnings: list[str] = []
    for finding in verdict.findings:
        if finding.category == "retrieved_prompt_injection":
            warnings.append("External content containing instructions aimed at the model was excluded.")
        elif finding.category == "retrieved_sensitive_data":
            warnings.append("Sensitive values in external content were redacted.")
    if verdict.findings:
        ctx.run.guardrail_verdicts.append(verdict)
    per_item = max(600, max_chars // max(1, len(safe))) if safe else 0
    rendered = "\n\n".join(
        wrap_untrusted("document", _clip(item["content"], per_item), index=index, source=item.get("url") or item.get("title"))
        for index, item in enumerate(safe, start=1)
    )
    return rendered, safe, warnings


def provider_unavailable(agent_label: str) -> AgentResult:
    return AgentResult(
        summary=f"{agent_label} could not generate an answer because no model provider is configured.",
        warnings=["Set LLM_PROVIDER=groq and GROQ_API_KEY to enable model responses."],
        metadata={"generation_mode": "unavailable"},
    )
