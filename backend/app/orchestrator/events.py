"""Batched, non-blocking event and step-state persistence.

Agents and the DAG executor emit events at a high rate. Writing each one in its
own transaction on the event loop was the dominant latency cost of the
original implementation. The sink queues writes and a single background task
flushes everything queued in one transaction in a worker thread, preserving
order. `flush()` is awaited at run boundaries so persisted state is complete
before the response returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.services.runtime_store import RuntimeStore

logger = logging.getLogger("orchestrator.events")


class EventSink:
    # Emits arriving within this window are written in the same transaction.
    COALESCE_SECONDS = 0.01

    def __init__(self, store: RuntimeStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self._pending: list[tuple[str, Any]] = []
        self._wakeup = asyncio.Event()
        self._closed = False
        self._writer: asyncio.Task[None] | None = None
        self._flushed = asyncio.Event()
        self._flushed.set()

    def start(self) -> "EventSink":
        if self._writer is None:
            self._writer = asyncio.create_task(self._run(), name=f"events:{self.run_id}")
        return self

    def emit(self, event_type: str, **fields: Any) -> None:
        self._enqueue(("event", {"type": event_type, **fields}))

    def update_run(self, **values: Any) -> None:
        self._enqueue(("run", values))

    def step(self, step_id: str, **values: Any) -> None:
        if "depends_on" in values:
            values["depends_on_json"] = json.dumps(values.pop("depends_on"))
        if "error" in values:
            error = values.pop("error")
            values["error_json"] = json.dumps(error, default=str) if error is not None else None
        self._enqueue(("step", (step_id, values)))

    def _enqueue(self, item: tuple[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("Event sink is closed.")
        self._pending.append(item)
        self._flushed.clear()
        self._wakeup.set()

    async def flush(self) -> None:
        # `_flushed` is cleared on every enqueue and only set once the queue is
        # empty AND no batch is in flight.
        if self._flushed.is_set():
            return
        if self._writer is None or self._writer.done():
            await self._write(self._drain())
            return
        self._wakeup.set()
        await self._flushed.wait()

    async def aclose(self) -> None:
        await self.flush()
        self._closed = True
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except asyncio.CancelledError:
                pass

    def _drain(self) -> list[tuple[str, Any]]:
        batch, self._pending = self._pending, []
        return batch

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            # Let bursts of emits coalesce into one transaction.
            await asyncio.sleep(self.COALESCE_SECONDS)
            batch = self._drain()
            if batch:
                await self._write(batch)
            elif not self._pending:
                self._flushed.set()

    async def _write(self, batch: list[tuple[str, Any]]) -> None:
        try:
            await asyncio.to_thread(self._write_sync, batch)
        except Exception:
            # Observability writes must never crash a workflow; surface loudly.
            logger.exception("event_persist_failed", extra={"run_id": self.run_id})
        finally:
            if not self._pending:
                self._flushed.set()

    def _write_sync(self, batch: list[tuple[str, Any]]) -> None:
        self.store.apply_batch(self.run_id, batch)
