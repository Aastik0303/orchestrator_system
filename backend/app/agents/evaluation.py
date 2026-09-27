from __future__ import annotations

import re

from app.config import get_settings
from app.models import AgentResult, EvaluationResult

WORD_RE = re.compile(r"[a-z0-9_]{3,}", re.IGNORECASE)
INLINE_CITATION_RE = re.compile(r"\[Source\s+\d+(?:\s*[,;:][^\]\n]{0,40})?\]", re.IGNORECASE)
STOP_WORDS = {
    "about",
    "after",
    "also",
    "from",
    "have",
    "into",
    "that",
    "the",
    "their",
    "this",
    "with",
    "your",
}


def evaluate_report(
    report: str,
    outputs: dict[str, AgentResult],
    *,
    request: str | None = None,
) -> EvaluationResult:
    normalized_report = report.strip()
    has_content = bool(normalized_report)
    warnings = list(dict.fromkeys(warning for result in outputs.values() for warning in result.warnings))
    expected_items = [
        text
        for result in outputs.values()
        for text in [result.summary, *result.findings, *result.recommendations]
        if text.strip()
    ]
    output_coverage = _content_coverage(normalized_report, expected_items)
    request_coverage = _term_coverage(normalized_report, request or "")

    if has_content:
        relevance = _clamp(
            45.0 + 30.0 * request_coverage + 25.0 * output_coverage
            if request
            else 65.0 + 35.0 * output_coverage
        )
        completeness = _clamp(45.0 + 55.0 * output_coverage)
    else:
        relevance = 0.0
        completeness = 0.0

    sources = [source for result in outputs.values() for source in result.sources]
    valid_sources = [
        source for source in sources if source.uri or source.document_id or source.chunk_id
    ]
    inline_citations = INLINE_CITATION_RE.findall(normalized_report)
    source_expected = bool(sources) or any(
        int(result.metadata.get("retrieved_chunks", 0) or 0) > 0 for result in outputs.values()
    )
    if not has_content:
        groundedness = 0.0
    elif sources:
        valid_ratio = len(valid_sources) / len(sources)
        if inline_citations:
            citation_ratio = min(1.0, len(inline_citations) / len(sources))
        elif "## Sources" in normalized_report:
            citation_ratio = 0.65
        else:
            citation_ratio = 0.0
        groundedness = _clamp(45.0 + 35.0 * valid_ratio + 20.0 * citation_ratio)
    elif source_expected:
        groundedness = 25.0
    else:
        groundedness = 82.0

    tool_calls = [
        result.metadata["tool_success"]
        for result in outputs.values()
        if "tool_success" in result.metadata
    ]
    tool_success_rate = (
        100.0 * sum(bool(value) for value in tool_calls) / len(tool_calls)
        if tool_calls
        else 100.0
    )
    failed_outputs = sum(
        1
        for result in outputs.values()
        if "failed" in result.summary.lower() or not result.summary.strip()
    )
    warning_penalty = min(32.0, 4.0 * len(warnings))
    correctness = (
        _clamp(
            94.0
            - warning_penalty
            - 18.0 * failed_outputs
            - (100.0 - tool_success_rate) * 0.2
            - (12.0 if source_expected and not valid_sources else 0.0)
        )
        if has_content
        else 0.0
    )

    has_required_heading = "## Executive Summary" in normalized_report
    balanced_code_fences = normalized_report.count("```") % 2 == 0
    format_valid = has_content and has_required_heading and balanced_code_fences
    overall = round(
        correctness * 0.30
        + relevance * 0.25
        + completeness * 0.20
        + groundedness * 0.25,
        1,
    )

    if not has_content or correctness < 55 or groundedness < 40:
        hallucination_risk = "high"
    elif correctness >= 80 and groundedness >= 75 and len(warnings) <= 1:
        hallucination_risk = "low"
    else:
        hallucination_risk = "medium"

    feedback: list[str] = []
    if request and request_coverage < 0.45:
        feedback.append("The report does not cover enough of the request's key terms.")
    if output_coverage < 0.7:
        feedback.append("Material agent findings are missing from the final report.")
    if source_expected and not valid_sources:
        feedback.append("Retrieved claims do not have valid source references.")
    elif sources and not inline_citations and "## Sources" not in normalized_report:
        feedback.append("Source-backed claims need inline citations or a Sources section.")
    if warnings:
        feedback.append("Resolve agent warnings before relying on the result for high-stakes use.")
    if not has_required_heading:
        feedback.append("The report is missing the Executive Summary heading.")
    if not balanced_code_fences:
        feedback.append("The report contains an unclosed Markdown code fence.")

    passing_score = get_settings().evaluation_passing_score
    return EvaluationResult(
        overall_score=overall,
        correctness=round(correctness, 1),
        relevance=round(relevance, 1),
        completeness=round(completeness, 1),
        groundedness=round(groundedness, 1),
        hallucination_risk=hallucination_risk,
        tool_success_rate=round(tool_success_rate, 1),
        format_valid=format_valid,
        retry_recommended=(
            overall < passing_score or not format_valid or hallucination_risk == "high"
        ),
        feedback=feedback,
    )


def _content_coverage(report: str, expected_items: list[str]) -> float:
    if not expected_items:
        return 1.0 if report else 0.0
    covered = sum(1 for item in expected_items if _term_coverage(report, item) >= 0.55)
    return covered / len(expected_items)


def _term_coverage(text: str, reference: str) -> float:
    reference_terms = _terms(reference)
    if not reference_terms:
        return 1.0 if text else 0.0
    text_terms = _terms(text)
    return len(reference_terms & text_terms) / len(reference_terms)


def _terms(value: str) -> set[str]:
    return {
        match.group(0).lower()
        for match in WORD_RE.finditer(value)
        if match.group(0).lower() not in STOP_WORDS
    }


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


# ---------------------------------------------------------------- agent spec

from app.agents.context import AgentContext  # noqa: E402
from app.agents.registry import ModelPolicy, RetryPolicy, register_agent  # noqa: E402
from app.models import AgentTask, StepArtifact  # noqa: E402


@register_agent(
    name="evaluation",
    description="Critic/validator: scores request coverage, evidence validity, completeness, groundedness, tools and format.",
    capabilities=["validation"],
    category="output",
    user_selectable=False,
    timeout_seconds=15,
    retry_policy=RetryPolicy(max_retries=0),
    model_policy=ModelPolicy(tier="none"),
    token_budget=0,
)
async def evaluation_agent(task: AgentTask, ctx: AgentContext) -> AgentResult:
    report_artifact = next((artifact for artifact in task.inputs.values() if artifact.metadata.get("report")), None)
    if report_artifact is None:
        report_artifact = next(iter(task.inputs.values()), None)
    if report_artifact is None:
        raise ValueError("Evaluation requires a report input.")
    raw_inputs = report_artifact.metadata.get("inputs", {})
    outputs = {}
    for step_id, payload in raw_inputs.items():
        artifact = StepArtifact.model_validate(payload)
        outputs[step_id] = AgentResult(
            summary=artifact.summary,
            findings=artifact.findings,
            recommendations=artifact.recommendations,
            sources=artifact.sources,
            warnings=artifact.warnings,
            metadata=artifact.metadata,
        )
    evaluation = evaluate_report(report_artifact.summary, outputs, request=task.goal)
    return AgentResult(
        summary=f"Quality score: {evaluation.overall_score:.1f}/100.",
        metadata={"evaluation": evaluation.model_dump(mode="json")},
    )
