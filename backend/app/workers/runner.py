"""Standalone worker process for WORKER_MODE=external.

    python -m app.workers.runner --concurrency 4

Claims queued runs from the shared database (atomic conditional UPDATE), runs
them with bounded concurrency and honours durable cancellation. Run as many
worker processes as needed; they coordinate only through the database.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import time
from uuid import uuid4

from app.agents.catalog import load_agents
from app.config import get_settings
from app.models import ChatRequest
from app.monitoring.logging import configure_logging
from app.orchestrator.executor import DynamicOrchestrator
from app.services.runtime_store import RuntimeStore, runtime_store
from app.workers.queue import persist_assistant_message

logger = logging.getLogger("orchestrator.worker")
HEARTBEAT_FILE = os.getenv("WORKER_HEARTBEAT_FILE", os.path.join(os.getenv("TMPDIR", "/tmp"), "orchestrator-worker.heartbeat"))


def _heartbeat() -> None:
    """Touch the heartbeat file; the container health check fails if it goes
    stale (a stuck claim loop), not merely if the process exists."""
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError:
        pass


async def execute_claimed(claimed: dict, store: RuntimeStore, orchestrator: DynamicOrchestrator) -> None:
    run_id = claimed["id"]
    try:
        request = ChatRequest.model_validate(claimed["request"])
    except Exception:
        logger.exception("invalid_queued_request", extra={"run_id": run_id})
        await asyncio.to_thread(store.finish_run, run_id, status="failed", error="Invalid queued request payload.")
        return
    response = None
    error: Exception | None = None
    try:
        response = await orchestrator.run(request, run_id=run_id, watch_cancellation=True)
    except Exception as exc:
        error = exc
        logger.exception("worker_run_failed", extra={"run_id": run_id})
    await persist_assistant_message(request.session_id, response, error, store)


async def worker_loop(
    concurrency: int,
    stop: asyncio.Event,
    store: RuntimeStore = runtime_store,
    *,
    max_runs: int | None = None,
) -> int:
    """Claim and execute runs until `stop` is set (or `max_runs` processed)."""
    load_agents()
    store.initialize()
    orchestrator = DynamicOrchestrator(store)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"
    poll = get_settings().worker_poll_seconds
    slots = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task[None]] = set()
    processed = 0
    logger.info("worker_started", extra={"status": worker_id})

    async def run(claimed: dict) -> None:
        try:
            await execute_claimed(claimed, store, orchestrator)
        finally:
            slots.release()

    while not stop.is_set() and (max_runs is None or processed < max_runs):
        _heartbeat()
        await slots.acquire()
        claimed = await asyncio.to_thread(store.claim_next_run, worker_id=worker_id)
        if not claimed:
            slots.release()
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass
            continue
        processed += 1
        task = asyncio.create_task(run(claimed), name=f"worker-run:{claimed['id']}")
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)
    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)
    await orchestrator.drain_background()
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrator background worker")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    configure_logging(get_settings().log_level)
    stop = asyncio.Event()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows
                signal.signal(sig, lambda *_: stop.set())
        await worker_loop(max(1, args.concurrency), stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
