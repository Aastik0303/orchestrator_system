"""PostgreSQL integration tests.

Skipped unless TEST_POSTGRES_URL is set, e.g.

    docker run -d --name orch-pg -e POSTGRES_DB=orchestrator -e POSTGRES_USER=orchestrator \
        -e POSTGRES_PASSWORD=pw -p 55432:5432 pgvector/pgvector:pg17
    set TEST_POSTGRES_URL=postgresql+psycopg://orchestrator:pw@127.0.0.1:55432/orchestrator

Re-runs the repository tests against a real PostgreSQL server, plus
PostgreSQL-specific checks.
"""

import _env  # noqa: F401  (must be first)

import os
import threading
import unittest

import test_store
from app.services.runtime_store import RuntimeStore, metadata

BASE_POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
# Tables are dropped between tests, so use a dedicated schema: the same server
# may also host the database used by the rest of the suite (TEST_DATABASE_URL).
SCHEMA = "orch_pgtest"
POSTGRES_URL = (
    f"{BASE_POSTGRES_URL}{'&' if '?' in BASE_POSTGRES_URL else '?'}options=-csearch_path%3D{SCHEMA}"
    if BASE_POSTGRES_URL
    else None
)


def setUpModule():
    if not BASE_POSTGRES_URL:
        return
    import sqlalchemy as sa

    engine = sa.create_engine(BASE_POSTGRES_URL)
    with engine.begin() as connection:
        connection.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    engine.dispose()


class PostgresStoreMixin:
    def setUp(self):
        self.store = RuntimeStore(POSTGRES_URL)
        metadata.drop_all(self.store.engine)
        self.store.initialize()

    def tearDown(self):
        metadata.drop_all(self.store.engine)
        self.store.close()


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL not set")
class PostgresChatScopingTests(PostgresStoreMixin, test_store.ChatScopingTests):
    pass


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL not set")
class PostgresRunStateTests(PostgresStoreMixin, test_store.RunStateTests):
    def test_claim_is_atomic_across_many_connections(self):
        for index in range(60):
            self._run(run_id=f"pg_run_{index}", request_json='{"message": "x"}')
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(worker_id: str) -> None:
            store = RuntimeStore(POSTGRES_URL)
            while True:
                item = store.claim_next_run(worker_id=worker_id)
                if item is None:
                    return
                with lock:
                    claimed.append(item["id"])

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(claimed), 60)
        self.assertEqual(len(set(claimed)), 60, "a run was claimed twice")

    def test_batch_writes_and_vector_search(self):
        self._run()
        self.store.apply_batch(
            "run_a",
            [
                ("event", {"type": "a"}),
                ("step", ("s1", {"status": "RUNNING", "agent": "x"})),
                ("step", ("s1", {"status": "SUCCESS", "attempts": 1})),
                ("run", {"active_agent": "x"}),
                ("event", {"type": "b"}),
            ],
        )
        self.assertEqual([event["type"] for event in self.store.list_events("run_a")], ["a", "b"])
        self.assertEqual(self.store.list_steps("run_a")[0]["status"], "SUCCESS")
        document = self.store.create_document(user_id="user-a", project_id="p", name="d", content_type=None, storage_path="x", size=1)
        self.store.update_document(document["id"], status="indexed", embedding_model="m")
        self.store.replace_chunks(
            document["id"],
            [
                {"id": "c1", "chunk_index": 0, "content": "alpha", "embedding": [1.0, 0.0, 0.0]},
                {"id": "c2", "chunk_index": 1, "content": "beta", "embedding": [0.0, 1.0, 0.0]},
            ],
        )
        results = self.store.search_chunks([0.9, 0.1, 0.0], user_id="user-a", project_id="p", top_k=1, threshold=0.0, embedding_model="m")
        self.assertEqual(results[0]["id"], "c1")
        self.assertEqual(self.store.search_chunks([1, 0, 0], user_id="user-b", project_id="p", top_k=5, threshold=0.0), [])


@unittest.skipUnless(POSTGRES_URL, "TEST_POSTGRES_URL not set")
class PgvectorSearchTests(PostgresStoreMixin, unittest.TestCase):
    def test_pgvector_matches_numpy_ranking_and_isolates_users(self):
        import numpy as np

        self.assertTrue(self.store.vector_enabled, "pgvector should be enabled on PostgreSQL")
        rng = np.random.default_rng(3)
        vectors = rng.standard_normal((300, 384)).astype(np.float32)
        for user in ("user-a", "user-b"):
            document = self.store.create_document(user_id=user, project_id="p", name=user, content_type=None, storage_path="x", size=1)
            self.store.update_document(document["id"], status="indexed", embedding_model="m")
            self.store.replace_chunks(
                document["id"],
                [{"id": f"{user}-{i}", "chunk_index": i, "content": f"chunk {i}", "embedding": vectors[i]} for i in range(300)],
            )
        query = vectors[42] + 0.05 * rng.standard_normal(384).astype(np.float32)
        via_pgvector = self.store.search_chunks(query.tolist(), user_id="user-a", project_id="p", top_k=5, threshold=-1, embedding_model="m")
        self.store.vector_enabled = False
        via_numpy = self.store.search_chunks(query.tolist(), user_id="user-a", project_id="p", top_k=5, threshold=-1, embedding_model="m")
        self.store.vector_enabled = True
        self.assertEqual(via_pgvector[0]["id"], "user-a-42")
        self.assertEqual([item["id"] for item in via_pgvector], [item["id"] for item in via_numpy])
        self.assertTrue(all(item["user_id"] == "user-a" for item in via_pgvector))
        for left, right in zip(via_pgvector, via_numpy):
            self.assertAlmostEqual(left["similarity"], right["similarity"], places=4)


if __name__ == "__main__":
    unittest.main()
