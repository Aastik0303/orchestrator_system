from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from fastapi import Request

from app.observability.telemetry import (
    current_run_id,
    current_session_id,
    current_trace_id,
    new_trace_id,
)

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"gsk_[A-Za-z0-9_-]+"), "[redacted]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+"), r"\1[redacted]"),
    (
        re.compile(
            r"(?i)(api[_-]?key|password|passwd|secret|access[_-]?token)([\"']?\s*[:=]\s*[\"']?)[^\s,\"']+"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"(?i)(\w+(?:\+\w+)?://[^:/\s@]+:)[^@\s]+(@)"), r"\1[redacted]\2"),
)
TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def redact(value: str) -> str:
    redacted = value
    for pattern, replacement in SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    try:
        from app.config import get_settings

        for secret in get_settings().secret_values():
            redacted = redacted.replace(secret, "[redacted]")
    except Exception:  # pragma: no cover - settings unavailable during early startup
        pass
    return redacted


class JsonFormatter(logging.Formatter):
    FIELDS = (
        "request_id",
        "node_id",
        "duration_ms",
        "status_code",
        "method",
        "path",
        "agent",
        "step_id",
        "status",
        "error_type",
        "latency_ms",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, var in (
            ("run_id", current_run_id),
            ("session_id", current_session_id),
            ("trace_id", current_trace_id),
        ):
            value = getattr(record, key, None) or var.get()
            if value:
                payload[key] = value
        for key in self.FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


async def request_logger(request: Request, call_next):
    started = perf_counter()
    incoming_id = request.headers.get("x-request-id") or ""
    request_id = incoming_id if REQUEST_ID_RE.match(incoming_id) else f"req_{uuid4().hex[:12]}"
    traceparent = TRACEPARENT_RE.match(request.headers.get("traceparent", ""))
    trace_id = traceparent.group(1) if traceparent else new_trace_id()
    request.state.request_id = request_id
    request.state.trace_id = trace_id
    token = current_trace_id.set(trace_id)
    try:
        response = await call_next(request)
    finally:
        current_trace_id.reset(token)
    response.headers["x-request-id"] = request_id
    response.headers["x-trace-id"] = trace_id
    # Only metadata is logged: never bodies, query strings or headers.
    logging.getLogger("orchestrator.http").info(
        "request_completed",
        extra={
            "request_id": request_id,
            "trace_id": trace_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": int((perf_counter() - started) * 1000),
        },
    )
    return response
