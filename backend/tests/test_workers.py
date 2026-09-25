import _env  # noqa: F401  (must be first)

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.models import ChatRequest
from app.services.runtime_store import RuntimeStore
from app.workers.queue import ExternalRunQueue, InProcessRunQueue
from app.workers.runner import worker_loop


class ExternalWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.temp_dir.name) / "queue.db")
        self.store.initialize()

    async def asyncTearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    async def test_api_enqueues_and_worker_executes(self):
        queue = ExternalRunQueue(self.store)
        session = self.store.create_chat_session(user_id="u")
        await queue.submit("run_q1", ChatRequest(message="hello there", user_id="u", session_id=session["id"]))
        self.assertEqual(self.store.get_run("run_q1", user_id="u")["status"], "queued")
        processed = await asyncio.wait_for(worker_loop(2, asyncio.Event(), self.store, max_runs=1), timeout=20)
        self.assertEqual(processed, 1)
        run = self.store.get_run("run_q1", user_id="u")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.store.list_chat_messages(session["id"], user_id="u")[-1]["role"], "assistant")

    async def test_cancelled_queued_run_is_never_executed(self):
        queue = ExternalRunQueue(self.store)
        await queue.submit("run_q2", ChatRequest(message="hello there", user_id="u"))
        self.assertEqual(await queue.cancel("run_q2", user_id="u"), "cancelled")
        self.assertIsNone(self.store.claim_next_run(worker_id="w"))
        self.assertEqual(self.store.get_run("run_q2", user_id="u")["status"], "cancelled")

    async def test_in_process_queue_runs_in_background(self):
        queue = InProcessRunQueue(self.store)
        from app.orchestrator import langgraph_flow
        from app.orchestrator.executor import DynamicOrchestrator
        from unittest.mock import patch

        orchestrator = DynamicOrchestrator(self.store)
        with patch.object(langgraph_flow, "orchestrator", orchestrator):
            await queue.submit("run_ip", ChatRequest(message="hello there", user_id="u"))
            for _ in range(200):
                if self.store.get_run("run_ip", user_id="u")["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(self.store.get_run("run_ip", user_id="u")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
