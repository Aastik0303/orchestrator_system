"""YouTube agent: answers questions about a video from its transcript.

The transcript is split into timestamped windows; for long videos the
windows sharing the most terms with the question are kept (or, for summary
requests, windows spread evenly over the video) within a character budget.
Transcript text is untrusted: it is guarded and delimited like any document,
and the model cites windows as [Source N].
"""

from __future__ import annotations

import re

from app.agents.common import build_user_prompt, render_evidence
from app.agents.context import AgentContext
from app.agents.prompts import system_prompt
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, RoutingHints, register_agent
from app.core.errors import OrchestratorError
from app.guardrails.retrieval_guard import validate_citations
from app.mcp.youtube import extract_video_ids
from app.models import AgentResult, AgentTask, SourceReference

ROLE = (
    "You are the YouTube Agent. Answer only from the supplied transcript <document> "
    "windows; each is labelled with its start time. Cite windows as [Source N] and "
    "mention timestamps where useful. If the transcript does not answer the question, "
    "say so instead of guessing."
)
WINDOW_SECONDS = 60
MAX_CONTEXT_CHARS = 10_000
TERM_RE = re.compile(r"[a-z0-9]{4,}")
STOP_TERMS = {"what", "that", "this", "with", "about", "does", "from", "video", "youtube", "have", "they", "there", "https", "watch"}


def _timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _windows(segments: list[dict]) -> list[dict]:
    windows: list[dict] = []
    for segment in segments:
        if not windows or segment["start"] - windows[-1]["start"] >= WINDOW_SECONDS:
            windows.append({"start": segment["start"], "texts": []})
        windows[-1]["texts"].append(" ".join(segment["text"].split()))
    return [{"start": window["start"], "text": " ".join(window["texts"])} for window in windows]


def select_windows(windows: list[dict], question: str, max_chars: int = MAX_CONTEXT_CHARS) -> list[dict]:
    """Deterministic selection of transcript windows, in chronological order."""
    if sum(len(window["text"]) for window in windows) <= max_chars:
        return windows
    terms = {term for term in TERM_RE.findall(question.lower()) if term not in STOP_TERMS}
    scored = [(len(terms & set(TERM_RE.findall(window["text"].lower()))), index) for index, window in enumerate(windows)]
    if terms and any(score for score, _ in scored):
        order = [index for _, index in sorted(scored, key=lambda item: (-item[0], item[1]))]
    else:  # summary-style question: spread the budget over the whole video
        per_window = max(1, sum(len(window["text"]) for window in windows) // max_chars + 1)
        order = list(range(0, len(windows), per_window))
    chosen: list[int] = []
    used = 0
    for index in order:
        size = len(windows[index]["text"])
        if used + size > max_chars:
            continue
        chosen.append(index)
        used += size
    return [windows[index] for index in sorted(chosen)]


@register_agent(
    name="youtube_rag",
    description="Answers questions about a YouTube video from its transcript, with timestamped citations.",
    capabilities=["youtube_transcript"],
    tools=["youtube.get_transcript"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=1),
    model_policy=ModelPolicy(tier="quality", temperature=0.2, max_output_tokens=1200),
    token_budget=10000,
    permission_policy=PermissionPolicy(granted_permissions={"web:read"}),
    routing_hints={
        "youtube_transcript": RoutingHints(
            description="Answer questions about a YouTube video.",
            url_patterns=[r"(?:youtube\.com/(?:watch\?|shorts/|embed/|live/)|youtu\.be/)"],
        )
    },
)
async def youtube_rag(task: AgentTask, ctx: AgentContext) -> AgentResult:
    video_ids = extract_video_ids(task.goal)
    if not video_ids:
        return AgentResult(
            summary="A YouTube link was detected, but it does not contain a valid 11-character video id.",
            warnings=["Share a full link such as https://www.youtube.com/watch?v=<id> or https://youtu.be/<id>."],
            metadata={"tool_success": False},
        )
    if not ctx.run.tools.is_available("youtube.get_transcript"):
        return AgentResult(
            summary="A YouTube video was linked, but transcript retrieval is not available on this server.",
            warnings=["Install youtube-transcript-api and set YOUTUBE_TRANSCRIPTS_ENABLED=true."],
            metadata={"tool_success": False},
        )
    video_id = video_ids[0]
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        transcript = await ctx.call_tool("youtube.get_transcript", {"video_id": video_id})
    except OrchestratorError as exc:
        return AgentResult(
            summary=f"The transcript of {url} could not be retrieved: {str(exc)[:200]}",
            warnings=["Answers about this video need a transcript (captions)."],
            metadata={"tool_success": False, "error_type": exc.error_type.value},
        )
    windows = select_windows(_windows(transcript["segments"]), task.instruction or task.goal)
    items = [
        {"title": f"{_timestamp(window['start'])}", "url": f"{url}&t={int(window['start'])}s", "content": f"[{_timestamp(window['start'])}] {window['text']}"}
        for window in windows
    ]
    evidence, kept, warnings = render_evidence(items, ctx, max_chars=MAX_CONTEXT_CHARS + 2000)
    sources = [SourceReference(title=f"YouTube {video_id} at {item['title']}", source_type="video", uri=item["url"]) for item in kept]
    metadata = {
        "video_id": video_id,
        "transcript_language": transcript.get("language"),
        "auto_generated": transcript.get("generated"),
        "windows_used": len(kept),
        "tool_success": bool(kept),
    }
    if not kept:
        return AgentResult(summary=f"The transcript of {url} is empty.", warnings=warnings, metadata=metadata)
    response = await ctx.llm(
        system=system_prompt(ROLE),
        user=build_user_prompt(task, include_conversation=False) + "\n\nTranscript windows:\n" + evidence,
    )
    if response is None:
        excerpt = "\n".join(f"- {item['content'][:300]}" for item in kept[:8])
        warnings.append("No model is configured; showing transcript excerpts without synthesis.")
        return AgentResult(summary=f"Transcript excerpts from {url}:\n\n{excerpt}", sources=sources, warnings=warnings, metadata=metadata)
    answer, citation_warnings = validate_citations(response.text, len(kept))
    return AgentResult(
        summary=answer,
        sources=sources,
        warnings=list(dict.fromkeys([*warnings, *citation_warnings])),
        metadata={**metadata, "model": response.model, "tokens": response.total_tokens},
    )
