"""Lightweight tracing for runs.

Each run owns a `Telemetry` recorder. Spans capture latency, token usage,
retry counts, tool counts, model and outcome for: API requests, guardrails,
routing, planning, agents, LLM calls, tool calls, retrieval, database calls and
synthesis. The active recorder and span are tracked in contextvars (per asyncio
task, inherited by `asyncio.to_thread`), never in module globals.

Spans are persisted to the `run_spans` table and aggregated by
`/api/metrics/latency` to find the top latency contributors.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from app.core.errors import classify_exception

logger = logging.getLogger("orchestrator.trace")

current_telemetry: contextvars.ContextVar["Telemetry | None"] = contextvars.ContextVar(
    "current_telemetry", default=None
)
current_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_span_id", default=None
)
# Correlation identifiers picked up by the JSON log formatter.
current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
current_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)
current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)

SPAN_KINDS = {
    "api",
    "guardrail",
    "routing",
    "planning",
    "agent",
    "llm",
    "tool",
    "retrieval",
    "db",
    "synthesis",
    "evaluation",
    "memory",
}


def new_trace_id() -> str:
    return uuid4().hex


@dataclass
class Span:
    span_id: str
    parent_id: str | None
    kind: str
    name: str
    started_at: str
    started_monotonic: float
    latency_ms: float = 0.0
    status: str = "success"
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, **attributes: Any) -> None:
        self.attributes.update({key: value for key, value in attributes.items() if value is not None})

    def add(self, key: str, amount: int | float) -> None:
        self.attributes[key] = self.attributes.get(key, 0) + amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "started_at": self.started_at,
            "latency_ms": round(self.latency_ms, 2),
            "status": self.status,
            "attributes": self.attributes,
        }


class Telemetry:
    """Collects spans for one run. Not shared across runs."""

    MAX_SPANS = 2000

    def __init__(self, *, run_id: str | None = None, trace_id: str | None = None) -> None:
        self.run_id = run_id
        self.trace_id = trace_id or new_trace_id()
        self.spans: list[Span] = []
        self.dropped_spans = 0

    def _open(self, kind: str, name: str, attributes: dict[str, Any]) -> Span:
        span = Span(
            span_id=uuid4().hex[:16],
            parent_id=current_span_id.get(),
            kind=kind,
            name=name,
            started_at=datetime.now(timezone.utc).isoformat(),
            started_monotonic=time.perf_counter(),
            attributes={key: value for key, value in attributes.items() if value is not None},
        )
        return span

    def _close(self, span: Span, error: BaseException | None) -> None:
        span.latency_ms = (time.perf_counter() - span.started_monotonic) * 1000
        if error is not None:
            error_type, retryable = classify_exception(error)
            span.status = "cancelled" if error_type.value == "CANCELLED" else "failed"
            span.set(error_type=error_type.value, retryable=retryable)
        if len(self.spans) < self.MAX_SPANS:
            self.spans.append(span)
        else:
            self.dropped_spans += 1

    @contextmanager
    def span_sync(self, kind: str, name: str, **attributes: Any) -> Iterator[Span]:
        span = self._open(kind, name, attributes)
        token = current_span_id.set(span.span_id)
        error: BaseException | None = None
        try:
            yield span
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised
            error = exc
            raise
        finally:
            current_span_id.reset(token)
            self._close(span, error)

    @asynccontextmanager
    async def span(self, kind: str, name: str, **attributes: Any):
        span = self._open(kind, name, attributes)
        token = current_span_id.set(span.span_id)
        error: BaseException | None = None
        try:
            yield span
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised
            error = exc
            raise
        finally:
            current_span_id.reset(token)
            self._close(span, error)

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, float]] = {}
        tokens = 0
        llm_calls = 0
        tool_calls = 0
        retries = 0
        for span in self.spans:
            bucket = by_kind.setdefault(span.kind, {"count": 0, "total_ms": 0.0, "max_ms": 0.0})
            bucket["count"] += 1
            bucket["total_ms"] += span.latency_ms
            bucket["max_ms"] = max(bucket["max_ms"], span.latency_ms)
            if span.kind == "llm":
                llm_calls += 1
                tokens += int(span.attributes.get("total_tokens", 0) or 0)
            if span.kind == "tool":
                tool_calls += 1
            retries += int(span.attributes.get("retry_count", 0) or 0)
        top = sorted(
            (span for span in self.spans if span.kind not in {"api"}),
            key=lambda item: item.latency_ms,
            reverse=True,
        )[:5]
        return {
            "trace_id": self.trace_id,
            "span_count": len(self.spans),
            "dropped_spans": self.dropped_spans,
            "tokens": tokens,
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
            "retries": retries,
            "by_kind": {
                kind: {
                    "count": int(values["count"]),
                    "total_ms": round(values["total_ms"], 1),
                    "max_ms": round(values["max_ms"], 1),
                }
                for kind, values in sorted(by_kind.items())
            },
            "top_spans": [
                {"kind": span.kind, "name": span.name, "latency_ms": round(span.latency_ms, 1)}
                for span in top
            ],
        }


@contextmanager
def maybe_span(kind: str, name: str, **attributes: Any) -> Iterator[Span | None]:
    """Record a span if a telemetry recorder is active in this context."""
    telemetry = current_telemetry.get()
    if telemetry is None:
        yield None
        return
    with telemetry.span_sync(kind, name, **attributes) as span:
        yield span


@contextmanager
def bind_run(telemetry: Telemetry, *, run_id: str | None, session_id: str | None) -> Iterator[None]:
    tokens = [
        current_telemetry.set(telemetry),
        current_run_id.set(run_id),
        current_session_id.set(session_id),
        current_trace_id.set(telemetry.trace_id),
    ]
    try:
        yield
    finally:
        current_trace_id.reset(tokens[3])
        current_session_id.reset(tokens[2])
        current_run_id.reset(tokens[1])
        current_telemetry.reset(tokens[0])
