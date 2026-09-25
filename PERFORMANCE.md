# Performance

All numbers were measured on the development machine (Windows 11, 16 logical
CPUs, Python 3.13) on 2026-09-25. They are comparable only with each other.
Raw results: `backend/benchmarks/results/*.json`.

No paid model was called. The "LLM" is the offline fake provider with a fixed
simulated latency, so the numbers isolate **orchestration overhead and
concurrency behavior**, not model speed.

## How to reproduce

```powershell
cd backend
# end-to-end HTTP benchmark: 1 / 10 / 25 / 50 concurrent requests
..\.venv\Scripts\python.exe -m benchmarks.load_test --label mine
..\.venv\Scripts\python.exe -m benchmarks.load_test --label mine-llm300 --fake-llm-latency-ms 300
..\.venv\Scripts\python.exe -m benchmarks.load_test --label mine-r4 --replicas 4   # 4 API processes, one DB
..\.venv\Scripts\python.exe -m benchmarks.load_test --label mine-pg --replicas 4 --database-url postgresql+psycopg://user:pw@127.0.0.1:5432/db
# micro-benchmarks (DAG parallelism, vector search; Postgres rows need MICRO_POSTGRES_URL)
..\.venv\Scripts\python.exe -m benchmarks.micro
# benchmark the original code (pass a directory that contains its `app` package)
..\.venv\Scripts\python.exe -m benchmarks.load_test --label baseline --app-dir <path-to-old-backend>
```

The load test starts isolated uvicorn server(s) with temporary storage, sends
a mix of four requests (chit-chat, code, research, multi-agent report), and
reports p50/p95/p99, error rate and throughput per concurrency level.

## Method: measure → find the contributor → fix → re-measure

| # | Measurement | Finding | Change | Effect |
|---|---|---|---|---|
| 1 | Baseline load test | **100% HTTP 502**: memory save needs an embedding package that was not installed and failed completed runs | Memory is optional context and best-effort; embedding failures are cached for 60 s | 0% errors |
| 2 | cProfile of one baseline run (186 ms) | ~170 ms in ~20 fresh SQLite connections (connect + PRAGMA + commit + close ≈ 8.5 ms each) **on the event loop** | Pooled SQLAlchemy engine, pragmas once per connection, DB I/O via `asyncio.to_thread`, schema init once | in-process run: 186 → 37 ms wall (measured after steps 2–3 together) |
| 3 | Span summary at 50 concurrent runs | Write-lock contention: ~25 small transactions per run (`upsert_step` 41 ms avg ×294, `append_events` 29 ms ×403) | Event sink batches events + step states + run updates into one transaction (10 ms coalescing); single-statement run start; report + evaluation + final status in one transaction | ~25 → ~5 transactions per run |
| 4 | CPU per run (inline profile) | 28.1 ms CPU/run, dominated by building a SQLAlchemy statement per row | `executemany` with one compiled statement per table; step updates merged per step | **18.8 ms** CPU/run |
| 5 | Trace of chit-chat requests | An extra sequential LLM routing call for greetings | Conversation keywords route deterministically; LLM routing only for low-confidence messages with ≥ 4 words | one model call removed from those requests |
| 6 | Micro-benchmark | Retrieval decoded JSON vectors and ran Python cosine per row | float32 blobs + NumPy (only ids/vectors scanned, text fetched for top-k); pgvector HNSW on PostgreSQL | 29–31× (SQLite), up to 60× (PostgreSQL) |
| 7 | First request after start | 226 ms to build the routing catalog | Warm-up at startup | removed from the first request |
| 8 | PostgreSQL span summary | ~2 ms per round trip through Docker Desktop; ~10 transactions per run | Fewer round trips (step 3) | 1 process 21 → 25 req/s; 4 replicas 35 → 48 req/s |

## End-to-end HTTP results on SQLite (`/api/chat`)

### Zero simulated LLM latency (pure orchestration overhead)

"Before" is the original code with only the fatal memory bug patched (the
unpatched original fails 100% of requests). Same harness for all rows.

| Concurrency | Before p50 / p95 / p99 (ms) | Before req/s | After, 1 process p50 / p95 / p99 | After req/s | After, 4 replicas p50 / p95 / p99 | 4-replica req/s |
|---|---|---|---|---|---|---|
| 1  | 110 / 219 / 219 | 6.5 | **21 / 34 / 34** | **45.3** | 22 / 38 / 38 | 43.4 |
| 10 | 1,509 / 1,796 / 1,796 | 6.3 | **101 / 306 / 306** | **65.1** | 81 / 220 / 220 | 74.2 |
| 25 | 3,636 / 5,266 / 5,321 | 6.6 | **310 / 681 / 756** | **65.5** | 197 / 476 / 685 | 72.8 |
| 50 | 6,657 / 12,788 / 13,870 | 6.6 | **688 / 1,476 / 1,743** | **56.8** | 386 / 1,155 / 1,329 | 74.2 |

Error rate: 0% in every "after" row (original, unpatched: 100%).

### 300 ms simulated LLM latency per call

| Concurrency | 1 process p50 / p95 / p99 | req/s | 4 replicas p50 / p95 / p99 | req/s |
|---|---|---|---|---|
| 1  | 325 / 336 / 336 | 3.1 | 325 / 337 / 337 | 3.1 |
| 10 | 371 / 510 / 510 | 23.9 | 347 / 423 / 423 | 26.5 |
| 25 | 431 / 730 / 817 | 45.4 | 372 / 585 / 743 | 50.5 |
| 50 | 746 / 1,289 / 1,591 | 48.3 | 480 / 1,112 / 1,324 | 63.6 |

At c=1 the overhead on top of the 300 ms model call is ~25 ms. The original
code has no simulated provider, so no "before" row exists for this table.

## End-to-end HTTP results on PostgreSQL

PostgreSQL 17 + pgvector in Docker Desktop (`pgvector/pgvector:pg17`) on the
same machine, same harness (`--database-url`). The API processes run on the
host; every query crosses Docker Desktop's port forwarding into its VM, which
measured **~2 ms per round trip** (in-process SQLite: microseconds). Pool
pre-ping on vs off made no measurable difference (25.1 vs 24.9 req/s at c=50),
so it stays on for stale-connection safety.

### Zero simulated LLM latency

| Concurrency | 1 process p50 / p95 / p99 (ms) | req/s | 4 replicas p50 / p95 / p99 | req/s |
|---|---|---|---|---|
| 1  | 43 / 63 / 63 | 21.9 | 45 / 62 / 62 | 20.7 |
| 10 | 309 / 701 / 701 | 25.1 | 150 / 309 / 309 | 52.2 |
| 25 | 768 / 1,739 / 1,789 | 25.4 | 386 / 949 / 1,033 | 43.3 |
| 50 | 1,669 / 2,891 / 3,745 | 24.9 | 739 / 1,919 / 2,073 | 47.6 |

### 300 ms simulated LLM latency

| Concurrency | 1 process p50 / p95 / p99 | req/s | 4 replicas p50 / p95 / p99 | req/s |
|---|---|---|---|---|
| 1  | 337 / 354 / 354 | 2.9 | 338 / 352 / 352 | 2.9 |
| 10 | 518 / 768 / 768 | 17.8 | 426 / 507 / 507 | 21.4 |
| 25 | 879 / 1,355 / 1,674 | 22.2 | 504 / 1,003 / 1,037 | 34.5 |
| 50 | 1,756 / 2,767 / 3,832 | 24.2 | 827 / 1,912 / 2,494 | 38.8 |

Error rate 0% in every row. Before the round-trip reductions, the same setup
measured 21.0 req/s (1 process) and 35.2 req/s (4 replicas) at c=50 with zero
LLM latency.

## Full Docker stack (`docker compose`, API + PostgreSQL + Redis in containers)

The containerized API talks to PostgreSQL container-to-container (measured
round trip **0.22 ms**, vs ~2 ms host → Docker VM) and, unlike the host runs
above, uses the **real embedding model** baked into the image, so every
planned run embeds its memory query and every completed run embeds a
workflow-memory record.

| Measurement | Finding | Change |
|---|---|---|
| Container load test, trace summary | Best-effort `save_workflow_memory` (~625 ms under load) ran **before** the response was returned | Memory saves now run as a bounded background task after the response (drained at shutdown) |
| Embedding calls under concurrency | torch used one intra-op thread per core per call; 10 concurrent callers: 13.7 ms/call at 16 threads vs **8.0 ms at 1 thread** | `EMBEDDING_TORCH_THREADS=1` default |

| Concurrency | Before fixes p50 / req/s | After p50 / p95 / p99 (ms) | After req/s |
|---|---|---|---|
| 1  | 75 ms / 12.5 | 60 / 101 / 101 | 15.1 |
| 10 | 639 ms / 14.1 | 377 / 780 / 780 | 20.1 |
| 25 | 1,627 ms / 13.4 | 931 / 1,829 / 1,981 | 21.4 |
| 50 | 3,482 ms / 13.0 (43% HTTP 429) | 1,991 / 3,273 / 3,911 | 20.9 |

Error rate after: 0%. The "before" run's 429s came from the stack's default
rate limit (30 requests/min per user) being exceeded by the benchmark's five
users, i.e. the limiter working as designed; the "after" run raises the limit
through a test-only compose override. A single embedding takes ~11 ms inside
the container; the higher in-trace latency under load is queueing for one
process's CPU/GIL (tokenization holds the GIL). With real embeddings one API
process tops out near ~21 req/s on this machine; scale out with replicas
(×1.9 measured with 4 replicas on PostgreSQL above).

## Micro-benchmarks (`benchmarks.micro`)

Latest recorded run (`benchmarks/results/micro.json`).

| Benchmark | Before | After | Speedup |
|---|---|---|---|
| Two-agent plan, each agent 300 ms: sequential (`MAX_PARALLEL_STEPS=1`) vs parallel | 645 ms | **343 ms** | 1.9× |
| SQLite vector search, 1,000 chunks (legacy JSON + Python loop vs blob + NumPy, incl. DB) | 165 ms | **5.3 ms** | 31× |
| SQLite vector search, 5,000 chunks | 811 ms | **27.6 ms** | 29× |
| SQLite vector search, 20,000 chunks | 3,271 ms | **111 ms** | 29× |
| PostgreSQL vector search, 1,000 chunks (NumPy over the network vs pgvector HNSW) | 26.7 ms | **10.5 ms** | 2.5× |
| PostgreSQL vector search, 5,000 chunks | 106 ms | **7.2 ms** | 15× |
| PostgreSQL vector search, 20,000 chunks | 484 ms | **8.1 ms** | 60× |

With pgvector, search time stays roughly flat as the corpus grows (the HNSW
index ranks inside the database); both paths return the same top result
(verified in the benchmark and in `test_postgres.PgvectorSearchTests`).

## Where the time goes now

`GET /api/metrics/latency` aggregates persisted spans. After the changes,
under concurrency the top contributors are, in order: model calls (when a real
provider is used), database write transactions (on SQLite serialized by the
single-writer lock; on PostgreSQL bounded by round-trip latency), and memory
retrieval (an embedding call). Per run, the orchestrator issues about 4 write
transactions plus one per event batch.

## Scalability: what the numbers show, and what they don't

- **The event loop no longer serializes requests.** The original code was
  capped near 6.5 req/s by blocking database calls even with a zero-latency
  model.
- **SQLite is fastest for a single process; PostgreSQL scales out.** On this
  machine one process reaches ~57 req/s on SQLite (in-process, no network) vs
  ~25 req/s on PostgreSQL (2 ms per round trip through Docker Desktop, plus more
  driver CPU). Adding replicas: SQLite ×1.3 (57 → 74 req/s; every write takes
  one file lock), PostgreSQL ×1.9 (25 → 48 req/s). For multiple replicas or
  workers use PostgreSQL. On a real network (same VPC, sub-millisecond round
  trips) the per-process PostgreSQL cost is expected to be lower than measured
  here; that has not been measured.
- Any replica can serve any request and cancel any run; multi-process
  correctness (atomic run claiming, cross-replica cancellation, cancel-wins
  semantics) is covered by tests on both databases.
- uvicorn's own `--workers N` mode was not usable for measurement on Windows:
  under load its supervisor restarts workers with console-wide Ctrl+C events,
  which killed the server tree. The `--replicas` mode (independent processes)
  was used instead; on Linux, `--workers` or container replicas both work.
- The load generator runs on the same machine as the servers and competes for
  CPU; absolute numbers are conservative.
- An earlier intermediate run recorded lower throughput because server logs
  were written to a Windows console, which is slow and serialized. All numbers
  above were recorded with server output discarded, including the baseline
  (re-recorded with the same harness).

## Tuning knobs

`DATABASE_POOL_SIZE`, `DATABASE_POOL_PRE_PING`, `VECTOR_BACKEND`, `EMBEDDING_TORCH_THREADS`,
`MAX_PARALLEL_STEPS`, `RAG_MAX_CONTEXT_CHARS` / `RAG_TOP_K` (prompt size),
`GROQ_MODEL_FAST` (cheaper routing and code writing), `ROUTER_LLM_ENABLED`,
`LLM_CACHE_SIZE`, `REDIS_URL` (shared cache).
