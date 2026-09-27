"""Result aggregator / report synthesizer.

Deterministic aggregation of structured upstream results into a Markdown
report: no extra LLM round-trip on the critical path, so multi-agent runs pay
only for the specialist calls that run in parallel.
"""

from __future__ import annotations

from app.agents.context import AgentContext
from app.agents.registry import ModelPolicy, RetryPolicy, RoutingHints, register_agent
from app.agents.data_transform import render_download_blocks
from app.agents.visualization import charts_from_metadata, render_chart_blocks
from app.models import AgentResult, AgentTask, ChatRequest, StepArtifact


def artifact_to_result(artifact: StepArtifact) -> AgentResult:
    return AgentResult(
        summary=artifact.summary,
        findings=artifact.findings,
        recommendations=artifact.recommendations,
        sources=artifact.sources,
        artifacts=artifact.artifacts,
        warnings=artifact.warnings,
        metadata=artifact.metadata,
    )


def generate_report(request: ChatRequest | None, outputs: dict[str, AgentResult]) -> str:
    deduplicated_findings = list(
        dict.fromkeys(finding for result in outputs.values() for finding in result.findings)
    )
    recommendations = list(dict.fromkeys(item for result in outputs.values() for item in result.recommendations))
    warnings = list(dict.fromkeys(item for result in outputs.values() for item in result.warnings))
    sources = []
    seen_sources: set[tuple] = set()
    for result in outputs.values():
        for source in result.sources:
            key = (source.uri, source.document_id, source.page, source.chunk_id)
            if key not in seen_sources:
                seen_sources.add(key)
                sources.append(source)

    sections = ["## Executive Summary"]
    summaries = [result.summary.strip() for result in outputs.values() if result.summary.strip()]
    sections.append("\n\n".join(summaries) or "No agent output was produced.")
    if deduplicated_findings:
        sections.extend(["## Agent Findings", "\n".join(f"- {item}" for item in deduplicated_findings)])
    if recommendations:
        sections.extend(
            ["## Recommendations", "\n".join(f"{index}. {item}" for index, item in enumerate(recommendations, 1))]
        )
    if sources:
        rendered = []
        for index, source in enumerate(sources, start=1):
            location = source.uri or source.document_id or "internal source"
            page = f", page {source.page}" if source.page else ""
            rendered.append(f"{index}. {source.title or source.source_type}: {location}{page}")
        sections.extend(["## Sources", "\n".join(rendered)])
    if warnings:
        sections.extend(["## Limitations", "\n".join(f"- {item}" for item in warnings)])
    downloads = render_download_blocks([artifact for result in outputs.values() for artifact in result.artifacts])
    if downloads:
        sections.append(downloads)
    charts = [chart for result in outputs.values() for chart in charts_from_metadata(result.metadata)]
    if charts:
        sections.append(render_chart_blocks(charts))
    return "\n\n".join(sections).strip()


@register_agent(
    name="report_generator",
    description="Combines structured results from upstream agents into a sourced Markdown report.",
    capabilities=["report_synthesis"],
    category="output",
    user_selectable=False,
    timeout_seconds=15,
    retry_policy=RetryPolicy(max_retries=0),
    model_policy=ModelPolicy(tier="none"),
    token_budget=0,
    routing_hints={
        "report_synthesis": RoutingHints(
            description="Combine the results of other steps into a report.",
        )
    },
)
async def report_generator(task: AgentTask, ctx: AgentContext) -> AgentResult:
    task_inputs = {
        step_id: artifact
        for step_id, artifact in task.inputs.items()
        if artifact.agent != "memory" and artifact.status == "SUCCESS"
    }
    outputs = {step_id: artifact_to_result(artifact) for step_id, artifact in task_inputs.items()}
    missing = [step_id for step_id, artifact in task.inputs.items() if artifact.status != "SUCCESS" and artifact.agent != "memory"]
    report = generate_report(None, outputs)
    warnings = [f"Step {step_id} did not complete; its results are missing from this report." for step_id in missing]
    if warnings:
        report += "\n\n" + "\n".join(f"> {warning}" for warning in warnings)
    return AgentResult(
        summary=report,
        warnings=warnings,
        metadata={
            "report": True,
            "inputs": {step_id: artifact.model_dump(mode="json") for step_id, artifact in task_inputs.items()},
        },
    )
