"""Background run execution.

* `InProcessRunQueue` (WORKER_MODE=inprocess, default): runs execute as asyncio
  tasks in the API process. Simple, but a run dies with its process.
* `ExternalRunQueue` (WORKER_MODE=external): the API only persists the run with
  its request payload (status `queued`); one or more worker processes
  (`python -m app.workers.runner`) claim runs atomically from the database.
  API replicas and workers scale independently.

Cancellation is durable in both modes: the API sets `cancel_requested` in the
database and whichever process executes the run observes it within ~0.5 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Protocol

from app.config import get_settings
from app.models import AgentResponse, ChatRequest
from app.services.runtime_store import RuntimeStore, runtime_store

logger = logging.getLogger("orchestrator.queue")

CompletionHandler = Callable[[AgentResponse | None, Exception | None], Awaitable[None] | None]


class RunQueue(Protocol):
    async def submit(self, run_id: str, request: ChatRequest, *, trace_id: str | None = None) -> None: ...

    async def cancel(self, run_id: str, *, user_id: str) -> str | None: ...


async def persist_assistant_message(
    session_id: str | None,
    response: AgentResponse | None,
    error: Exception | None,
    store: RuntimeStore = runtime_store,
) -> None:
    if not session_id:
        return
    if response is not None:
        content = response.response
    else:
        content = "The workflow failed. Check the run events for the recorded error."
    try:
        await asyncio.to_thread(store.add_chat_message, session_id, "assistant", content)
    except Exception:
        logger.exception("assistant_message_persist_failed")


class InProcessRunQueue:
    def __init__(self, store: RuntimeStore = runtime_store) -> None:
        self.store = store
        # Handles to tasks running in THIS process (a cache for fast local
        # cancellation; the database flag remains the source of truth).
        self._tasks: dict[str, tuple[asyncio.Task[Any], asyncio.Event]] = {}

    async def submit(self, run_id: str, request: ChatRequest, *, trace_id: str | None = None) -> None:
        from app.orchestrator.langgraph_flow import run_orchestrator

        if run_id in self._tasks:
            raise ValueError(f"Run {run_id} is already active.")
        await asyncio.to_thread(
            self.store.create_run,
            run_id=run_id,
            user_id=request.user_id,
            project_id=request.project_id,
            session_id=request.session_id,
            task=request.message,
            file_count=len(request.files),
            trace_id=trace_id,
        )
        cancel_event = asyncio.Event()

        async def execute() -> None:
            response: AgentResponse | None = None
            error: Exception | None = None
            try:
                response = await run_orchestrator(
                    request, run_id=run_id, cancel_event=cancel_event, watch_cancellation=True
                )
            except Exception as exc:
                error = exc
                logger.exception("background_run_failed", extra={"run_id": run_id})
            finally:
                await persist_assistant_message(request.session_id, response, error, self.store)

        task = asyncio.create_task(execute(), name=f"workflow:{run_id}")
        self._tasks[run_id] = (task, cancel_event)
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))

    async def cancel(self, run_id: str, *, user_id: str) -> str | None:
        status = await asyncio.to_thread(self.store.request_cancel, run_id, user_id=user_id)
        local = self._tasks.get(run_id)
        if local is not None and status in {"cancelling", "cancelled"}:
            local[1].set()
        return status

    def is_active(self, run_id: str) -> bool:
        local = self._tasks.get(run_id)
        return bool(local and not local[0].done())


class ExternalRunQueue:
    def __init__(self, store: RuntimeStore = runtime_store) -> None:
        self.store = store

    async def submit(self, run_id: str, request: ChatRequest, *, trace_id: str | None = None) -> None:
        await asyncio.to_thread(
            self.store.create_run,
            run_id=run_id,
            user_id=request.user_id,
            project_id=request.project_id,
            session_id=request.session_id,
            task=request.message,
            file_count=len(request.files),
            trace_id=trace_id,
            request_json=json.dumps(request.model_dump(mode="json")),
        )

    async def cancel(self, run_id: str, *, user_id: str) -> str | None:
        return await asyncio.to_thread(self.store.request_cancel, run_id, user_id=user_id)


@lru_cache
def get_run_queue() -> RunQueue:
    if get_settings().worker_mode.lower() == "external":
        return ExternalRunQueue()
    return InProcessRunQueue()
