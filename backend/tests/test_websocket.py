import _env  # noqa: F401  (must be first)

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.websocket import router
from app.services.runtime_store import RuntimeStore, utc_now


class WebSocketEventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.temp_dir.name) / "runtime.db")
        self.store.initialize()
        self.store.create_run(run_id="run_ws", user_id="local-user", project_id="default", session_id=None, task="Stream events", file_count=0)
        self.app = FastAPI()
        self.app.include_router(router)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_persisted_events_are_streamed_until_terminal_status(self):
        self.store.append_event("run_ws", "workflow_started", node_id="input", node_type="input", label="User Request", status="completed")
        self.store.append_event("run_ws", "workflow_completed", node_id="final", status="completed")
        self.store.update_run("run_ws", status="completed", completed_at=utc_now())
        with patch("app.routes.websocket.runtime_store", self.store), TestClient(self.app) as client:
            with client.websocket_connect("/ws/runs/run_ws") as websocket:
                received = [websocket.receive_json() for _ in range(3)]
        self.assertEqual([event["type"] for event in received], ["workflow_started", "workflow_completed", "stream_closed"])

    def test_other_users_cannot_subscribe(self):
        from starlette.websockets import WebSocketDisconnect

        with patch("app.routes.websocket.runtime_store", self.store), TestClient(self.app) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/ws/runs/run_ws?user_id=intruder") as websocket:
                    websocket.receive_json()


if __name__ == "__main__":
    unittest.main()
