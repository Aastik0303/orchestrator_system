import _env  # noqa: F401  (must be first)

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.services.chat_store import add_message, create_session, delete_session, ensure_session, get_session, list_messages, list_sessions
from app.services.runtime_store import RuntimeStore


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(Path(self.temp_dir.name) / "runtime.db")
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def _run(self, run_id="run_a", user_id="user-a", **kwargs):
        return self.store.create_run(
            run_id=run_id, user_id=user_id, project_id="p", session_id=None, task="task", file_count=0, **kwargs
        )


class ChatScopingTests(StoreTestCase):
    def test_delete_session_removes_its_messages(self):
        session = create_session("Delete me", user_id="user-a", store=self.store)
        add_message(session["id"], "user", "Temporary message", store=self.store)
        self.assertTrue(delete_session(session["id"], user_id="user-a", store=self.store))
        self.assertIsNone(get_session(session["id"], user_id="user-a", store=self.store))
        self.assertEqual(list_messages(session["id"], user_id="user-a", store=self.store), [])
        self.assertFalse(delete_session(session["id"], user_id="user-a", store=self.store))

    def test_sessions_are_isolated_between_users(self):
        session = create_session("Private", user_id="user-a", store=self.store)
        add_message(session["id"], "user", "my private note", store=self.store)
        self.assertEqual(len(list_sessions(user_id="user-a", store=self.store)), 1)
        self.assertEqual(list_sessions(user_id="user-b", store=self.store), [])
        self.assertEqual(list_messages(session["id"], user_id="user-b", store=self.store), [])
        self.assertFalse(delete_session(session["id"], user_id="user-b", store=self.store))
        self.assertIsNotNone(get_session(session["id"], user_id="user-a", store=self.store))

    def test_ensure_session_never_attaches_to_foreign_session(self):
        session = create_session("A", user_id="user-a", store=self.store)
        hijack = ensure_session(session["id"], "B", user_id="user-b", store=self.store)
        self.assertNotEqual(hijack["id"], session["id"])


class RunStateTests(StoreTestCase):
    def test_events_steps_and_spans_round_trip(self):
        self._run()
        stored = self.store.append_events("run_a", [{"type": "a"}, {"type": "b", "details": {"x": 1}}])
        self.assertLess(stored[0]["sequence"], stored[1]["sequence"])
        self.assertEqual([event["type"] for event in self.store.list_events("run_a", after=stored[0]["sequence"])], ["b"])
        self.store.upsert_step("run_a", "s1", status="RUNNING", agent="x", depends_on_json='["m"]')
        self.store.upsert_step("run_a", "s1", status="SUCCESS", attempts=2)
        step = self.store.list_steps("run_a")[0]
        self.assertEqual((step["status"], step["attempts"], step["depends_on"]), ("SUCCESS", 2, ["m"]))
        self.store.save_spans(
            "run_a",
            [{"span_id": "s1", "kind": "llm", "name": "x", "status": "success", "latency_ms": 12.5, "attributes": {"total_tokens": 7}, "started_at": "t"}],
        )
        summary = self.store.latency_summary(user_id="user-a")
        self.assertEqual(summary["contributors"][0]["tokens"], 7)
        self.assertEqual(self.store.latency_summary(user_id="user-b")["spans_analyzed"], 0)

    def test_finish_run_does_not_override_cancellation(self):
        self._run(status="running")
        self.assertEqual(self.store.request_cancel("run_a", user_id="user-a"), "cancelling")
        self.assertTrue(self.store.get_run_status("run_a")[1])
        self.store.update_run("run_a", status="cancelled")
        self.assertFalse(self.store.finish_run("run_a", status="completed"))
        self.assertEqual(self.store.get_run("run_a", user_id="user-a")["status"], "cancelled")

    def test_cancel_is_scoped_to_owner(self):
        self._run()
        self.assertIsNone(self.store.request_cancel("run_a", user_id="intruder"))
        self.assertEqual(self.store.request_cancel("run_a", user_id="user-a"), "cancelled")

    def test_claim_is_atomic_across_threads(self):
        for index in range(20):
            self._run(run_id=f"run_{index}", request_json='{"message": "x"}')
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(worker_id: str) -> None:
            while True:
                item = self.store.claim_next_run(worker_id=worker_id)
                if item is None:
                    return
                with lock:
                    claimed.append(item["id"])

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(claimed), 20)
        self.assertEqual(len(set(claimed)), 20, "a run was claimed twice")

    def test_active_run_count(self):
        self._run(run_id="r1", status="running")
        self._run(run_id="r2")
        self._run(run_id="r3", status="completed")
        self.assertEqual(self.store.count_active_runs(user_id="user-a"), 2)


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_sqlite_schema_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE workflow_runs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    session_id TEXT, task TEXT NOT NULL, status TEXT NOT NULL, active_agent TEXT, route_json TEXT,
                    plan_json TEXT, response TEXT NOT NULL DEFAULT '', artifacts_json TEXT NOT NULL DEFAULT '[]',
                    needs_clarification INTEGER NOT NULL DEFAULT 0, file_count INTEGER NOT NULL DEFAULT 0, error TEXT,
                    started_at TEXT NOT NULL, completed_at TEXT, duration_ms INTEGER NOT NULL DEFAULT 0);
                INSERT INTO workflow_runs (id, user_id, project_id, task, status, started_at)
                    VALUES ('old', 'local-user', 'default', 'legacy task', 'completed', '2026-01-01');
                CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT INTO chat_sessions VALUES ('chat_old', 'Old chat', '2026-01-01', '2026-01-01');
                CREATE TABLE chat_messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, attachment_name TEXT, created_at TEXT NOT NULL);
                INSERT INTO chat_messages VALUES ('m1', 'chat_old', 'user', 'hi', NULL, '2026-01-01');
                """
            )
            connection.commit()
            connection.close()
            store = RuntimeStore(path)
            try:
                store.initialize()
                run = store.get_run("old", user_id="local-user")
                self.assertEqual(run["task"], "legacy task")
                self.assertFalse(run["cancel_requested"])
                self.assertEqual(len(store.list_chat_sessions(user_id="local-user")), 1)
                store.upsert_step("old", "s", status="SUCCESS")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
