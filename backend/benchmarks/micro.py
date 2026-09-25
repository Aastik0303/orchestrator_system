"""Micro-benchmarks for specific optimizations (offline, deterministic).

1. DAG parallelism: a two-agent plan (each agent sleeps to simulate a 300 ms
   model call) executed with max_parallel_steps=4 vs 1 (sequential).
2. Vector search: the original per-row JSON-decode + pure-Python cosine loop
   vs the current float32-blob + NumPy implementation, at 1k/5k/20k chunks.

    python -m benchmarks.micro
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("GROQ_API_KEY", "")
_TMP = tempfile.mkdtemp(prefix="orchestrator-micro-")
os.environ["RUNTIME_DATABASE_PATH"] = str(Path(_TMP) / "runtime.db")
os.environ["STORAGE_ROOT"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging  # noqa: E402

logging.disable(logging.CRITICAL)

import numpy as np  # noqa: E402

from app.agents.catalog import load_agents  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models import AgentResult, ChatRequest  # noqa: E402
from app.orchestrator.executor import DynamicOrchestrator  # noqa: E402
from app.services.runtime_store import RuntimeStore, _json  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"


async def dag_parallelism(repeats: int = 5) -> dict:
    registry = load_agents()
    store = RuntimeStore(Path(_TMP) / "dag.db")
    orchestrator = DynamicOrchestrator(store)

    async def slow(summary: str):
        async def handler(task, ctx):
            await asyncio.sleep(0.3)
            return AgentResult(summary=summary)

        return handler

    results = {}
    for label, parallel in (("parallel", "4"), ("sequential", "1")):
        os.environ["MAX_PARALLEL_STEPS"] = parallel
        get_settings.cache_clear()
        timings = []
        with registry.override("code_dev", await slow("code")), registry.override("deep_research", await slow("research")):
            for _ in range(repeats):
                started = time.perf_counter()
                response = await orchestrator.run(
                    ChatRequest(message="Analyze this repository and research current patterns", user_id="micro")
                )
                assert response.status == "completed", response.failures
                timings.append((time.perf_counter() - started) * 1000)
        results[label] = {"median_ms": round(statistics.median(timings), 1), "runs": repeats}
    os.environ.pop("MAX_PARALLEL_STEPS", None)
    get_settings.cache_clear()
    store.close()
    results["speedup"] = round(results["sequential"]["median_ms"] / results["parallel"]["median_ms"], 2)
    return results


def _legacy_search(rows: list[tuple[str, str]], query: list[float], top_k: int) -> list[tuple[str, float]]:
    """The original implementation: JSON-decode every row, Python cosine."""
    def cosine(left, right):
        dot = sum(a * b for a, b in zip(left, right))
        ln = math.sqrt(sum(v * v for v in left))
        rn = math.sqrt(sum(v * v for v in right))
        return dot / (ln * rn) if ln and rn else 0.0

    scored = [(chunk_id, cosine(query, json.loads(embedding))) for chunk_id, embedding in rows]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


def vector_search(sizes=(1000, 5000, 20000), dims: int = 384) -> list[dict]:
    rng = np.random.default_rng(7)
    output = []
    for size in sizes:
        store = RuntimeStore(Path(_TMP) / f"vectors_{size}.db")
        document = store.create_document(user_id="u", project_id="p", name="d", content_type=None, storage_path="x", size=1)
        store.update_document(document["id"], status="indexed", embedding_model="m")
        vectors = rng.standard_normal((size, dims)).astype(np.float32)
        store.replace_chunks(
            document["id"],
            [{"id": f"c{i}", "chunk_index": i, "content": f"chunk {i}", "embedding": vectors[i]} for i in range(size)],
        )
        query = rng.standard_normal(dims).astype(np.float32).tolist()
        legacy_rows = [(f"c{i}", _json(vectors[i].tolist())) for i in range(size)]

        started = time.perf_counter()
        legacy = _legacy_search(legacy_rows, query, 5)
        legacy_ms = (time.perf_counter() - started) * 1000

        timings = []
        for _ in range(3):
            started = time.perf_counter()
            current = store.search_chunks(query, user_id="u", project_id="p", top_k=5, threshold=-1.0, embedding_model="m")
            timings.append((time.perf_counter() - started) * 1000)
        assert [item["id"] for item in current] == [item[0] for item in legacy], "implementations disagree"
        output.append(
            {
                "chunks": size,
                "legacy_python_ms": round(legacy_ms, 1),
                "numpy_blob_ms_including_db": round(statistics.median(timings), 1),
                "speedup": round(legacy_ms / statistics.median(timings), 1),
            }
        )
        store.close()
        print(json.dumps(output[-1]), flush=True)
    return output


def postgres_vector_search(url: str, sizes=(1000, 5000, 20000), dims: int = 384) -> list[dict]:
    """PostgreSQL: NumPy path (all candidate vectors fetched over the network)
    vs pgvector (HNSW, ranked in the database)."""
    from sqlalchemy import text

    from app.services.runtime_store import metadata

    rng = np.random.default_rng(11)
    output = []
    for size in sizes:
        store = RuntimeStore(url)
        metadata.drop_all(store.engine)
        store._initialized = False
        store.initialize()
        document = store.create_document(user_id="u", project_id="p", name="d", content_type=None, storage_path="x", size=1)
        store.update_document(document["id"], status="indexed", embedding_model="m")
        vectors = rng.standard_normal((size, dims)).astype(np.float32)
        store.replace_chunks(
            document["id"],
            [{"id": f"c{i}", "chunk_index": i, "content": f"chunk {i}", "embedding": vectors[i]} for i in range(size)],
        )
        with store.begin() as connection:
            connection.execute(text("ANALYZE document_chunks"))
        query = vectors[7].tolist()
        timings: dict[str, float] = {}
        ids: dict[str, list[str]] = {}
        for mode, enabled in (("postgres_numpy_ms", False), ("postgres_pgvector_ms", True)):
            store.vector_enabled = enabled
            samples = []
            for _ in range(5):
                started = time.perf_counter()
                result = store.search_chunks(query, user_id="u", project_id="p", top_k=5, threshold=-1.0, embedding_model="m")
                samples.append((time.perf_counter() - started) * 1000)
            timings[mode] = round(statistics.median(samples), 1)
            ids[mode] = [item["id"] for item in result]
        output.append(
            {
                "chunks": size,
                **timings,
                "speedup": round(timings["postgres_numpy_ms"] / timings["postgres_pgvector_ms"], 1),
                "same_top1": ids["postgres_numpy_ms"][0] == ids["postgres_pgvector_ms"][0],
            }
        )
        metadata.drop_all(store.engine)
        store.close()
        print(json.dumps(output[-1]), flush=True)
    return output


def main() -> None:
    dag = asyncio.run(dag_parallelism())
    print(json.dumps({"dag_parallelism": dag}), flush=True)
    vectors = vector_search()
    postgres_url = os.getenv("MICRO_POSTGRES_URL")
    postgres = postgres_vector_search(postgres_url) if postgres_url else None
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "dag_parallelism": dag,
        "vector_search": vectors,
        "postgres_vector_search": postgres,
    }
    (RESULTS_DIR / "micro.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {RESULTS_DIR / 'micro.json'}")


if __name__ == "__main__":
    main()
