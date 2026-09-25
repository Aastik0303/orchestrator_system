# Architecture

## Request pipeline

```text
User
 │  HTTP (FastAPI) · auth principal · rate limit · active-run limit · upload validation
 ▼
Input Guardrail ───── block → safe refusal (no model, tool or retrieval work)
 ▼
Intent Router  (1) hard rules  (2) capability matching  (3) LLM only if not confident, validated
 ▼
Planner ─────── ExecutionPlan (DAG) validated against the Agent Registry and limits
 ▼
Execution engine (DAG) ── parallel ready steps · timeouts · classified retries · budgets · cancellation
 ▼
Agent Registry ──> Agents ──> AgentContext.llm()  (model gateway: budget, tiers, retries, telemetry)
                         └──> AgentContext.call_tool() → Tool permission guard → Tool Registry → tool / sandbox
 ▼
Result Aggregator (report_generator, deterministic)
 ▼
Critic / Validator (evaluation)
 ▼
Output Guardrail ── redact secrets/PII · block system-prompt leakage
 ▼
Final response + persisted run, steps, events, spans, report, evaluation
```

Code map:

| Stage | Module |
|---|---|
| API, auth, limits | `app/routes/*`, `app/security/auth.py`, `app/security/rate_limit.py` |
| Guardrails | `app/guardrails/` (`detectors`, `input_guard`, `output_guard`, `retrieval_guard`) |
| Router | `app/orchestrator/router.py`, `app/orchestrator/capabilities.py` |
| Planner | `app/orchestrator/planner.py`, `planner_validator.py` |
| DAG engine | `app/orchestrator/dag.py`, `events.py` |
| Pipeline | `app/orchestrator/executor.py` |
| Agent Registry / specs | `app/agents/registry.py`, `catalog.py`, `context.py`, one module per agent |
| Tool Registry | `app/mcp/registry.py`, `schemas.py`, `permissions.py` |
| Sandbox | `app/sandbox/python_runner.py` |
| Model gateway | `app/llm/client.py` |
| Persistence | `app/services/runtime_store.py` (SQLAlchemy Core) |
| Workers | `app/workers/queue.py`, `runner.py` |
| Telemetry | `app/observability/telemetry.py`, `app/monitoring/logging.py` |

## Agent Registry

Every agent is an `AgentSpec`:

| Field | Meaning |
|---|---|
| `name`, `description` | identity |
| `capabilities` | what it provides (`data_analysis`, `web_research`, `document_retrieval`, `youtube_transcript`, `code_generation`, `python_execution`, `sql_query`, `conversation`, `report_synthesis`, `validation`, `memory_retrieval`) |
| `handler` | `async (AgentTask, AgentContext) -> AgentResult` |
| `tools` | explicit tool allowlist (qualified names) |
| `timeout_seconds` | per-attempt timeout |
| `retry_policy` | max retries, exponential backoff base/max, jitter, retryable error types |
| `model_policy` | tier (`none` / `fast` / `quality`), temperature, max output tokens |
| `token_budget` | per-step token cap (in addition to the run budget) |
| `permission_policy` | granted permissions (e.g. `vector:read`, `code:execute`) |
| `routing_hints` | optional keywords / file types / URL patterns / `suppresses`, per capability |

Adding an agent = one new module in `app/agents/` with `@register_agent(...)`.
`catalog.load_agents()` discovers it; the router picks up its routing hints; a
brand-new capability needs no router change (see
`test_new_agent_with_new_capability_routes_without_router_changes`).

## Routing

Structured output (`RoutingDecision`): `intent`, `required_capabilities`,
`candidate_agents`, `primary_agent`, `secondary_agents`, `confidence`,
`reason`, `requires_planning`, `strategy`, `scores`.

1. **Hard rules** (confidence 0.99): manual override (task agents only,
   validated against the registry), attached file types (datasets before
   documents), YouTube URLs.
2. **Capability matching**: weighted keyword scores per capability; secondary
   capabilities are kept when they score ≥ 30% of the top; `suppresses`
   removes redundant capabilities (running code does not also need a coder);
   chit-chat scores as `conversation` without a model call.
3. **LLM routing** only when stages 1–2 fall back with low confidence and the
   message has ≥ 4 words. The model chooses from the catalog; unknown or
   system capabilities are discarded, confidence is capped at 0.9 and must meet
   `ROUTER_CONFIDENCE_THRESHOLD`; otherwise the fallback stands.

## Planning

`create_execution_plan` builds a DAG:

- Single capability and not complex → one step.
- Otherwise: `memory_1` (optional context, dependents use `all_done`) → one step
  per capability. The message is split into clauses at sequencing markers
  ("then", "after that", "based on the results"...). Capabilities in the same
  clause run in parallel; a later clause depends on the previous clause.
- `report_1` (aggregator) depends on all task steps with `any_success`
  (partial results are reported with limitations).
- `evaluation_1` (critic) depends on the report.

```text
"Analyze this CSV and research the market, then create a report"

memory_1 ──┬──> data_analyst_2 ──┐
           └──> deep_research_1 ─┴──> report_1 ──> evaluation_1
```

Plans are validated for duplicate ids, missing dependencies, unknown agents,
cycles, `MAX_PLAN_NODES` and `MAX_PLAN_DEPTH`.

## DAG execution and agent lifecycle

Step states: `PENDING → RUNNING → (RETRYING → RUNNING)* → SUCCESS | FAILED |
TIMEOUT | BLOCKED | CANCELLED`.

- Ready steps start immediately (bounded by `MAX_PARALLEL_STEPS`).
- Dependency modes: `all_success` (default), `all_done` (optional inputs),
  `any_success` (synthesis). Unsatisfiable dependents become `BLOCKED` with a
  structured reason; nothing is silently skipped.
- Each attempt: `timeout = min(step/agent timeout, remaining run time)`.
- Errors are classified (`TRANSIENT`, `PERMANENT`, `SECURITY`,
  `INVALID_INPUT`, `RATE_LIMIT`, `TIMEOUT`, `MODEL_ERROR`, `TOOL_ERROR`,
  `BUDGET_EXCEEDED`, `CANCELLED`). Only retryable failures are retried, with
  exponential backoff + jitter, bounded by the agent policy **and** the
  run-wide retry pool.
- Approval denials and permission failures end as `BLOCKED`.
- Every failure is a `StructuredFailure`:
  `{"agent": "research", "status": "timeout", "error_type": "TIMEOUT", "retryable": true, ...}`.

Budgets per run (`Budget`): max steps, max runtime, max tool calls, max
tokens, max LLM calls, max retries. Exhaustion raises `BudgetExceeded` and the
run stops safely with a structured failure.

Cancellation: `POST /api/runs/{id}/stop` sets a durable `cancel_requested`
flag; the executing process (API replica or worker) polls it every 0.5 s,
cancels running steps and marks pending ones `CANCELLED`. A cancel always wins
over a late completion (`finish_run` checks the flag atomically).

## Data passed between agents

Agents never receive raw concatenated conversations. Each step gets an
`AgentTask`: the goal, its step instruction, attached files, structured
upstream `StepArtifact`s (summary, findings, sources, warnings, status),
scoped memory snippets and the last few conversation turns. Prompt rendering
(`agents/common.py`) bounds each section (`STEP_INPUT_MAX_CHARS`) and wraps all
non-user content in untrusted delimiters.

## State separation

| State | Where | Lifetime |
|---|---|---|
| Request state | `RunContext` (budget, telemetry, cancel event) | one run, in memory of the executing process |
| Workflow state | `workflow_runs`, `run_steps`, `execution_events` | durable |
| Execution history / traces | `run_spans`, `execution_events`, `reports`, `evaluations` | durable |
| Short-term conversation | `chat_sessions`, `chat_messages` (user-scoped) | durable |
| Long-term memory | `memories` (user + project scoped; sensitive content rejected) | durable |
| Files | `STORAGE_ROOT/uploads`, `outputs/<user-hash>/` | durable (volume / object storage) |
| Cache, rate limits | in-memory, or Redis when `REDIS_URL` is set | ephemeral |
| Connection pools, compiled catalogs | per process | process |

No module-level mutable state participates in workflow decisions. Per-process
tool statistics are observability-only.

## Horizontal scaling

- API replicas are stateless: any replica can serve any request and cancel any
  run.
- `WORKER_MODE=external`: the API persists the request (`request_json`) and
  returns `202`; `python -m app.workers.runner` processes claim queued runs
  with an atomic conditional `UPDATE ... WHERE status='queued'` (verified with
  6 threads racing for 20 runs: no double claims).
- Use PostgreSQL (`DATABASE_URL=postgresql+psycopg://...`) for more than one
  process: SQLite serializes writers (see [PERFORMANCE.md](PERFORMANCE.md)).
- Redis only for shared cache and rate-limit counters.

## RAG pipeline

```text
Document → parser (pypdf / python-docx / text / openpyxl) → recursive chunking
→ embeddings (HF all-MiniLM-L6-v2) → float32 blobs (portable) + pgvector column on PostgreSQL
→ vector search, owner-scoped: pgvector HNSW in the database on PostgreSQL
  (`VECTOR_BACKEND=auto`), NumPy otherwise (only ids+vectors loaded); text fetched for top-k
→ absolute threshold (0.30, calibrated for MiniLM) + relative cutoff (≥ 50% of best score)
→ retrieval guardrail (quarantine instruction-bearing chunks, redact secrets/PII)
→ context builder (char budget, per-document cap, dedup, <document> delimiters)
→ LLM (grounded prompt; documents declared untrusted)
→ citation validation (drop citations to non-existent sources) + lexical faithfulness score
```

Irrelevant questions return "insufficient evidence" without a model call.
Stale-model documents are skipped (not re-embedded in the query path).

## Model gateway

`app/llm/client.py`: one shared provider client per process, async interface
(blocking SDK call in a worker thread), model tiers (`GROQ_MODEL_FAST` for
routing/code-writing, `GROQ_MODEL` for answers), budget reservation, token
accounting from provider usage, error classification with ≤ 3 jittered
attempts for retryable errors, optional cache for temperature-0 calls, and a
deterministic `fake` provider for tests and benchmarks.

## Observability

Every run has `run_id`, `session_id` and `trace_id` (propagated from a
`traceparent` header when present). Spans (`api`, `guardrail`, `routing`,
`planning`, `agent`, `llm`, `tool`, `retrieval`, `db`, `synthesis`, `memory`)
record `latency_ms`, tokens, retry count, model, status and error type; they
are persisted to `run_spans` and exposed at `/api/runs/{id}/trace` and
aggregated at `/api/metrics/latency`. JSON logs carry the ids from context
variables, log only metadata (never bodies) and pass through secret redaction.

## Environment variables

See [.env.example](.env.example) (every setting is documented there).
