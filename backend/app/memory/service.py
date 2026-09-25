"""Long-term memory.

Memory layers in the platform:
* short-term conversation state: chat sessions/messages (services.chat_store)
* working task state: per-run steps and artifacts (run_steps, StepArtifact)
* long-term memory: this module (memories table, user + project scoped)
* execution history: workflow_runs, execution_events, run_spans

Rules enforced here:
* retrieval is always filtered by the authenticated user (and project);
* content containing secrets, credentials or high-risk PII is never
  persisted automatically;
* memory failures never fail a workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.guardrails.detectors import contains_sensitive_data, redact_pii, redact_secrets
from app.rag.service import embedding_service
from app.services.runtime_store import RuntimeStore, runtime_store

logger = logging.getLogger("orchestrator.memory")


def retrieve_memory(
    query: str,
    *,
    user_id: str,
    project_id: str,
    store: RuntimeStore = runtime_store,
    limit: int = 6,
) -> list[dict[str, Any]]:
    memories = store.search_memories(
        embedding_service.embed(query),
        user_id=user_id,
        project_id=project_id,
        limit=limit,
    )
    for memory in memories:
        memory.pop("embedding", None)
    return memories


def save_memory_safely(
    content: str,
    *,
    user_id: str,
    project_id: str,
    run_id: str | None,
    session_id: str | None,
    memory_type: str = "workflow",
    importance_score: float = 0.65,
    store: RuntimeStore = runtime_store,
) -> dict[str, Any] | None:
    normalized = " ".join(content.split())
    if len(normalized) < 24:
        return None
    secrets = get_settings().secret_values()
    if contains_sensitive_data(normalized, secrets):
        logger.info("memory_skipped_sensitive")
        return None
    # Defense in depth: redact anything the detector did not treat as blocking.
    normalized, _ = redact_secrets(normalized, secrets)
    normalized, _ = redact_pii(normalized, strict=True)
    return store.save_memory(
        user_id=user_id,
        project_id=project_id,
        memory_type=memory_type,
        content=normalized[:2_000],
        importance_score=max(0.0, min(1.0, importance_score)),
        source_session_id=session_id,
        source_run_id=run_id,
        embedding=embedding_service.embed(normalized),
        metadata={"source": "completed_workflow" if memory_type == "workflow" else "tool"},
    )


def save_workflow_memory(
    content: str,
    *,
    user_id: str,
    project_id: str,
    run_id: str,
    session_id: str | None,
    importance_score: float = 0.65,
    store: RuntimeStore = runtime_store,
) -> dict[str, Any] | None:
    return save_memory_safely(
        content,
        user_id=user_id,
        project_id=project_id,
        run_id=run_id,
        session_id=session_id,
        importance_score=importance_score,
        store=store,
    )
