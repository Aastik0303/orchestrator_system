# Repository Audit

Audit of `orchestrator_system` **as found**, before the upgrade described in
[ARCHITECTURE.md](ARCHITECTURE.md). Every claim below was checked against the
code, a test run, a profile or a benchmark. Measured numbers live in
[PERFORMANCE.md](PERFORMANCE.md).

## 1. Current architecture (as found)

```text
React (Vite) ──HTTP/WS──> FastAPI routes ──> DynamicOrchestrator
                                              ├─ router.choose_route   keyword if/elif + optional LLM
                                              ├─ agents/planner        fixed template (memory → agents → report → eval)
                                              ├─ supervisor.AGENTS     hard-coded dict {AgentName: function}
                                              ├─ asyncio.gather over task agents (sync functions in threads)
                                              ├─ report_generator / evaluation (deterministic)
                                              └─ memory save (embeddings)
                                   RuntimeStore (raw sqlite3, new connection per call)
                                   chat_store   (separate raw sqlite3 module, not user-scoped)
                                   MCPRegistry  (tool metadata + a few handlers, never called by agents)
                                   RAG: pypdf/docx → LangChain splitter → HF embeddings → JSON vectors in SQLite
```

Infrastructure in `docker-compose.yml` (Postgres+pgvector, Redis) was declared
but **not used by the application**: the runtime always used SQLite, and the
Alembic migration described a different schema (users/projects/...) that no
code read or wrote.

## 2. Existing features

Hybrid-ish routing (manual override, file-type rules, keyword hints, LLM
fallback); template planning with plan validation (cycles, depth, node count);
parallel execution of task agents; persisted runs/events/reports/evaluations;
document upload, chunking, HF embeddings, cosine retrieval with a LangGraph RAG
subgraph; deterministic CSV/Excel profiling; evidence-based report scoring;
WebSocket event streaming; React Flow workspace, runs/agents/tools/evaluations
pages.

## 3. Working features (verified)

- 33 unit tests passed at the start.
- Plan validation, CSV profiling, report evaluation, chunking and the RAG graph
  (with mocked retrieval) worked as tested.
- Frontend built successfully.

## 4. Broken or incomplete features

| Finding | Evidence |
|---|---|
| **Every chat request failed (HTTP 502)** in the checked-in environment. After a run was already marked `completed`, the executor saved "workflow memory", which imports `langchain_huggingface`; the package (and torch) is not installed in `.venv`, the exception propagated and the completed run was re-marked `failed`. | Baseline benchmark: 100% `http_502`; traceback reproduced (`ModuleNotFoundError: langchain_huggingface` in `save_workflow_memory`). |
| Multi-agent requests failed for the same reason at the memory **retrieval** step. | Same root cause. |
| RAG retrieval silently re-embedded stale documents inside the query path. | `search_knowledge` called `index_document` for every stale document. |
| `youtube_rag`, `sql_agent` are stubs; web/GitHub tools have no handlers. | Code; kept and labelled honestly. |
| Tools were registered but **no agent ever called a tool**; tool permissions were never enforced on an agent path. | `MCPRegistry.execute` had no callers outside tests. |
| Cancellation only worked inside the process that started the run. | `RunManager._tasks` dict. |
| The Alembic migration did not match the runtime schema. | Table/column mismatch. |

## 5. Technical debt

- Closed `AgentName` enum + hard-coded `AGENTS` dict: adding an agent meant
  editing the enum, the dict, the router's if/elif chain and the capabilities
  table in `routes/runtime.py`.
- Two separate SQLite access layers (`runtime_store`, `chat_store`) with
  CWD-relative paths.
- Agents received the raw user message concatenated with memory text; no
  structured artifacts between steps.
- `Settings` read environment variables at import time (impossible to override
  in tests/workers without re-importing).
- `groq_completion` built a new HTTP client per call; no token accounting.
- Exceptions retried blindly (every exception, fixed count, no backoff).

## 6. Security risks

| Severity | Risk |
|---|---|
| Critical | **Identity was client-supplied**: `user_id` was a form/query field on every endpoint, so any caller could read or delete any user's runs, documents, memories and reports. |
| Critical | **Chat sessions were not scoped at all**: `GET /api/chat/sessions` listed every user's conversations; messages of any session id were readable. |
| High | **File tools could read/list any user's uploads**: `file.list_files` listed the shared upload directory; `read_file`/`inspect_dataset` accepted raw paths. |
| High | No input guardrails: prompt injection, jailbreaks, credential/system-prompt extraction reached the model. |
| High | No output guardrails: secrets or PII produced by a model were returned and persisted. |
| High | RAG injection filter was five literal substrings; retrieved text was pasted into the prompt without delimiters; secrets inside documents were sent to the model. |
| Medium | Workflow memory automatically persisted the raw request text (including any credentials a user typed). |
| Medium | Error messages (`Workflow failed: {error}`) were written into chat history. |
| Medium | No rate limiting or concurrent-run limits; uploads validated by extension only. |
| Medium | Backend container ran as root; Postgres credentials hard-coded in compose/alembic.ini. |

## 7. Latency bottlenecks (measured)

1. **Per-call SQLite connections on the event loop.** Each run opened ~20
   connections (`connect` + `PRAGMA journal_mode=WAL` + commit + close ≈ 8.5 ms
   each) synchronously inside `async` code: ~170 ms of a 186 ms run, and it
   serialized all concurrent requests (throughput flat at ~6.5 req/s at every
   concurrency level).
2. Schema DDL (`executescript`) re-run on most writes.
3. Retrieval decoded every stored vector from JSON and computed cosine in pure
   Python (O(N) Python loop; ~2 s at 5k chunks).
4. A new Groq client per model call (no connection reuse).
5. The LLM intent classifier ran as an extra **sequential** model call.

## 8. Scalability bottlenecks

- Process-local mutable state: in-memory run task registry (cancellation),
  tool statistics.
- SQLite as the only store; Postgres configured but unused.
- Blocking I/O inside async endpoints (DB, file writes).
- No worker abstraction: background runs die with the API process.

## 9. Missing tests

No tests for routing edge cases beyond a handful, security (injection,
exfiltration, cross-user access), sandbox/tool permissions, cancellation,
timeouts, budgets, API authentication, rate limiting, concurrency, RAG
relevance/faithfulness, or load.

## 10. Recommended architecture

Implemented in this upgrade (see [ARCHITECTURE.md](ARCHITECTURE.md)):

```text
User → API (auth, rate limits) → Input Guardrail → Intent Router (rules → capabilities → validated LLM)
     → Planner (DAG) → Execution engine (parallel, budgets, retries, cancellation)
     → Agent Registry → Agents + Tools (Tool Registry + permission guard + sandbox)
     → Result Aggregator → Critic/Validator → Output Guardrail → Response
Durable state: SQLAlchemy repository (SQLite locally, PostgreSQL in deployment)
Ephemeral: Redis (cache, rate limits) when configured · Workers: DB-backed queue
Observability: per-run traces (spans), structured logs with run/session/trace ids
```
