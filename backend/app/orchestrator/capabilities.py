"""Capability catalog used by the router.

Built-in definitions below are merged with `routing_hints` declared by agents
in the registry, so a new agent can introduce a new capability without
editing this file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.registry import AgentRegistry

# Capabilities that never come from user intent directly.
SYSTEM_CAPABILITIES = {"memory_retrieval", "validation"}
# Capabilities that only make sense combined with others (they consume
# upstream results).
SYNTHESIS_CAPABILITIES = {"report_synthesis"}
FALLBACK_CAPABILITY = "conversation"


@dataclass
class CapabilityDefinition:
    name: str
    description: str
    keywords: dict[str, float] = field(default_factory=dict)
    file_extensions: set[str] = field(default_factory=set)
    url_patterns: list[str] = field(default_factory=list)
    # Capabilities made redundant when this one is selected (e.g. running code
    # does not also need a code-writing agent).
    suppresses: set[str] = field(default_factory=set)
    _compiled: list[tuple[re.Pattern[str], float]] = field(default_factory=list, repr=False)
    _urls: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def compile(self) -> "CapabilityDefinition":
        self._compiled = [
            (re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])"), weight)
            for keyword, weight in self.keywords.items()
        ]
        self._urls = [re.compile(pattern, re.IGNORECASE) for pattern in self.url_patterns]
        return self

    def score(self, text: str) -> tuple[float, list[str]]:
        total = 0.0
        matched: list[str] = []
        for pattern, weight in self._compiled:
            if pattern.search(text):
                total += weight
                matched.append(pattern.pattern)
        return total, matched

    def matches_url(self, raw_text: str) -> bool:
        return any(pattern.search(raw_text) for pattern in self._urls)


BUILTIN_CAPABILITIES: dict[str, CapabilityDefinition] = {
    "data_analysis": CapabilityDefinition(
        name="data_analysis",
        description="Analyze and visualize tabular datasets (CSV/Excel): data quality, statistics, outliers, correlations, trends, charts.",
        keywords={
            "csv": 2.0,
            "excel": 2.0,
            "xlsx": 2.0,
            "spreadsheet": 2.0,
            "dataset": 2.0,
            "data set": 2.0,
            "data quality": 2.5,
            "missing values": 2.0,
            "outlier": 1.5,
            "outliers": 1.5,
            "correlation": 1.5,
            "correlations": 1.5,
            "statistics": 1.2,
            "kpi": 1.5,
            "metrics": 1.0,
            "analytics": 1.2,
            "pivot": 1.5,
            "columns": 1.0,
            "rows": 1.0,
            "trend": 0.8,
            "trends": 0.8,
            "sales": 1.0,
            "forecast": 1.0,
            "visualize": 1.5,
            "visualise": 1.5,
            "visualization": 1.5,
            "visualisation": 1.5,
            "chart": 1.2,
            "charts": 1.2,
            "graph": 1.0,
            "graphs": 1.0,
            "plot": 1.0,
            "histogram": 1.5,
            "distribution": 1.0,
            "heatmap": 1.2,
            "dashboard": 0.8,
            "data analysis": 2.5,
            "analyze data": 2.5,
            "analyze the data": 2.5,
            "data agent": 2.5,
            "table": 0.6,
        },
        file_extensions={".csv", ".xlsx", ".xls"},
    ),
    "web_research": CapabilityDefinition(
        name="web_research",
        description="Research current information, markets, competitors, news or trends.",
        keywords={
            "research": 2.0,
            "deep research": 2.5,
            "latest": 1.2,
            "current": 0.8,
            "news": 1.5,
            "market": 1.5,
            "competitor": 1.8,
            "competitors": 1.8,
            "compare": 1.0,
            "comparison": 1.0,
            "sources": 1.0,
            "industry": 1.0,
            "state of the art": 1.5,
            "look up": 1.2,
            "deep analysis": 1.5,
        },
    ),
    "document_retrieval": CapabilityDefinition(
        name="document_retrieval",
        description="Answer questions from the user's uploaded/indexed documents with citations.",
        keywords={
            "document": 1.5,
            "documents": 1.5,
            "pdf": 1.8,
            "docx": 1.8,
            "knowledge base": 2.0,
            "in the file": 1.5,
            "uploaded file": 1.0,
            "uploaded document": 2.0,
            "my notes": 1.5,
            "according to": 1.2,
            "the contract": 1.5,
            "the policy": 1.0,
            "cite": 1.0,
            "resume": 2.0,
            "resumes": 2.0,
            "résumé": 2.0,
            "cv": 1.5,
            "curriculum vitae": 2.0,
            "this file": 1.5,
            "attached file": 2.0,
            "attached document": 2.0,
            "uploaded": 1.0,
        },
        file_extensions={".pdf", ".docx", ".md", ".txt"},
    ),
    "code_generation": CapabilityDefinition(
        name="code_generation",
        description="Write, review, debug or explain code; software architecture.",
        keywords={
            "code": 1.2,
            "function": 1.0,
            "class": 0.6,
            "python": 1.2,
            "typescript": 1.5,
            "javascript": 1.5,
            "react": 1.5,
            "fastapi": 1.5,
            "langgraph": 1.2,
            "bug": 1.5,
            "debug": 1.8,
            "error": 0.8,
            "stack trace": 1.8,
            "exception": 1.0,
            "refactor": 1.8,
            "implement": 1.5,
            "repository": 1.8,
            "repo": 1.5,
            "github": 1.5,
            "api": 1.0,
            "unit test": 1.5,
            "architecture": 1.5,
            "backend": 1.2,
            "frontend": 1.2,
            "code review": 2.0,
            "algorithm": 1.2,
        },
    ),
    "sql_query": CapabilityDefinition(
        name="sql_query",
        description="Write or run read-only SQL against an approved database.",
        keywords={
            "sql query": 3.5,
            "sql": 2.0,
            "database query": 3.0,
            "postgres query": 3.0,
            "schema query": 3.0,
            "select * from": 3.0,
        },
    ),
    "report_synthesis": CapabilityDefinition(
        name="report_synthesis",
        description="Combine the results of other steps into a report or summary.",
        keywords={
            "report": 1.5,
            "create a report": 2.5,
            "write a report": 2.5,
            "summary report": 2.5,
            "summarize the findings": 2.0,
            "executive summary": 2.0,
            "write up": 1.5,
            "synthesize": 1.5,
        },
    ),
    "conversation": CapabilityDefinition(
        name="conversation",
        description="General conversation and questions that need no specialist.",
        # Scored only when no specialist capability matches, so chit-chat is
        # routed deterministically instead of paying for an LLM routing call.
        keywords={
            "hello": 2.0,
            "hi": 2.0,
            "hey": 2.0,
            "good morning": 2.0,
            "good evening": 2.0,
            "thanks": 2.0,
            "thank you": 2.0,
            "how are you": 2.0,
            "who are you": 2.0,
            "what can you do": 2.0,
            "what can you help": 2.0,
            "help me with": 1.0,
            "tell me a joke": 2.0,
        },
    ),
}


def build_catalog(registry: AgentRegistry) -> dict[str, CapabilityDefinition]:
    catalog: dict[str, CapabilityDefinition] = {}
    for name, definition in BUILTIN_CAPABILITIES.items():
        catalog[name] = CapabilityDefinition(
            name=name,
            description=definition.description,
            keywords=dict(definition.keywords),
            file_extensions=set(definition.file_extensions),
            url_patterns=list(definition.url_patterns),
            suppresses=set(definition.suppresses),
        )
    for name, hints in registry.routing_hints().items():
        definition = catalog.setdefault(name, CapabilityDefinition(name=name, description=hints.description or name))
        if hints.description and not definition.description:
            definition.description = hints.description
        definition.keywords.update(hints.keywords)
        definition.file_extensions |= hints.file_extensions
        definition.url_patterns.extend(hints.url_patterns)
        definition.suppresses |= hints.suppresses
    # Only capabilities with at least one registered agent are routable.
    provided = registry.capabilities()
    return {name: definition.compile() for name, definition in catalog.items() if name in provided}
