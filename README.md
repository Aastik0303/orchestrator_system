# AI Agent Orchestration Platform

FastAPI + React platform that routes a request to the right specialist agents,
plans a dependency graph, executes it in parallel under budgets, and returns a
validated, guardrailed answer with a full trace.

```text
User → API → Input Guardrail → Intent Router → Planner → Execution DAG → Agent Registry
     → Agents + Tools (permissioned, sandboxed) → Result Aggregator → Critic → Output Guardrail → Response
```

- **Agents** (discoverable by capability): data analysis (CSV/Excel profiling),
  web research, document RAG with citations, YouTube transcripts (adapter not
  configured), code generation, sandboxed Python execution (approval required),
  read-only SQL (disabled until configured), conversation; plus system agents
  for memory, report aggregation and evaluation.
- **Hybrid routing**: hard rules → capability matching → validated LLM routing.
- **DAG execution**: parallel independent steps, dependency tracking,
  timeouts, classified retries with backoff + jitter, cancellation, budgets
  (steps, runtime, tool calls, tokens, LLM calls, retries).
- **Security**: input/output/retrieval guardrails, per-agent tool allowlists,
  approval gates, a sandbox, API-key auth, per-user isolation, rate limits.
- **Observability**: run/session/trace ids, persisted spans (latency, tokens,
  retries, model, status), `/api/runs/{id}/trace`, `/api/metrics/latency`.

Docs: [ARCHITECTURE](ARCHITECTURE.md) · [SECURITY](SECURITY.md) ·
[EVALUATION](EVALUATION.md) · [PERFORMANCE](PERFORMANCE.md) ·
[AUDIT](AUDIT.md) (state of the repository before this upgrade) · [API](docs/api.md)

## Local development

```powershell
copy .env.example .env            # set GROQ_API_KEY, or LLM_PROVIDER=fake to run offline
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements-dev.txt
cd backend
uvicorn app.main:app --reload --port 8002
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev                       # http://127.0.0.1:5173
```

Notes:

- Without `DATABASE_URL`, state lives in SQLite at `RUNTIME_DATABASE_PATH`
  (existing local databases are upgraded in place on startup).
- Document RAG needs the embedding stack (`sentence-transformers`, torch) and
  the `all-MiniLM-L6-v2` model in the local Hugging Face cache
  (`EMBEDDING_LOCAL_FILES_ONLY=true`). Without it, uploads are stored but
  indexing reports a clear error and memory retrieval is skipped; every other
  feature keeps working.
- `AUTH_MODE=dev` trusts `X-User-Id` (local only). For anything shared, set
  `AUTH_MODE=api_key` and `API_KEYS`, and give the frontend `VITE_API_KEY`.
- Background workers: `WORKER_MODE=external` plus
  `python -m app.workers.runner --concurrency 4` (needs PostgreSQL when more
  than one process writes).
- Sandboxed execution requires explicit approval per request: send
  `approved_tools=sandbox.python_exec` with the chat request.

## Docker

```powershell
copy .env.example .env            # set POSTGRES_PASSWORD (required) and GROQ_API_KEY
docker compose up --build                     # UI :5173, API :8002, Postgres, Redis; runs migrations
docker compose --profile workers up --build   # + 2 background workers (set WORKER_MODE=external)
```

Verified end to end on 2026-09-25 (all services healthy): UI and API through
the nginx proxy, Alembic migrations, PostgreSQL + pgvector (document chunks
indexed with the baked-in embedding model), Redis-backed rate limiting,
background runs executed by both worker containers, durable cancellation of a
queued run, per-user isolation and WebSocket streaming. The backend image
installs CPU-only torch and includes the embedding model, so document search
works offline. With `GROQ_API_KEY` in `.env`, the stack calls Groq; set
`LLM_PROVIDER=fake` to try it without API costs.

## Testing

```powershell
cd backend
..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"    # 128 tests (unit, integration, security, RAG, API, workers, eval gate)
..\.venv\Scripts\python.exe -m evals.run                                     # evaluation suites + thresholds
..\.venv\Scripts\python.exe -m benchmarks.load_test --label local            # load test (1/10/25/50 concurrent)
..\.venv\Scripts\python.exe -m benchmarks.micro                              # DAG + retrieval micro-benchmarks

cd ..\frontend
npm run build
```

Tests run hermetically (temporary storage, fake LLM, deterministic embeddings,
no network). The suite is also pytest-compatible (`pytest backend/tests`).

Against PostgreSQL (e.g. `docker run -d -p 55432:5432 -e POSTGRES_USER=orchestrator
-e POSTGRES_PASSWORD=pw -e POSTGRES_DB=orchestrator pgvector/pgvector:pg17`):

```powershell
$env:TEST_POSTGRES_URL = "postgresql+psycopg://orchestrator:pw@127.0.0.1:55432/orchestrator"  # Postgres-specific tests
$env:TEST_DATABASE_URL = $env:TEST_POSTGRES_URL                                               # whole suite on Postgres
..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

## Adding an agent

Create `backend/app/agents/my_agent.py`:

```python
from app.agents.registry import RoutingHints, register_agent
from app.models import AgentResult


@register_agent(
    name="translator",
    description="Translates text between languages.",
    capabilities=["translation"],
    timeout_seconds=30,
    routing_hints={"translation": RoutingHints(description="Translate text", keywords={"translate": 2.5})},
)
async def translator(task, ctx):
    response = await ctx.llm(system="You translate text.", user=task.goal)
    return AgentResult(summary=response.text if response else "No model configured.")
```

No router, planner or API changes are needed.

## Current limitations

See the "limitations" sections of [SECURITY.md](SECURITY.md),
[PERFORMANCE.md](PERFORMANCE.md) and [EVALUATION.md](EVALUATION.md). In short:
the sandbox is defense in depth, not kernel-level isolation; web search, GitHub,
YouTube and SQL adapters are declared but not connected (they need credentials);
PostgreSQL numbers were measured through Docker Desktop on one machine, not on a
production network.
