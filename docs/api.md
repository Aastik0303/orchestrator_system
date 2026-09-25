# API Reference

Base URL: `/api`. WebSocket: `/ws/runs/{run_id}`. Health: `GET /health` (liveness), `GET /ready` (database reachable).

## Authentication

- `AUTH_MODE=api_key`: send `Authorization: Bearer <key>` or `X-API-Key: <key>`
  (WebSocket: `?api_key=<key>`). The user is derived from the key; a
  mismatching `user_id` returns 403. Missing/invalid keys return 401.
- `AUTH_MODE=dev` (local only): `X-User-Id` header or `user_id` parameter,
  default `local-user`.

Resources owned by another user return 404. Limits: 429 on rate limit
(`Retry-After` header) or too many active runs.

## Chat

- `POST /chat` (multipart): `message`, optional `files[]`, `session_id`,
  `project_id`, `agent_override` (task agent name or `auto`), `deep_research`,
  `approved_tools` (comma-separated, e.g. `sandbox.python_exec`). Returns
  `{status, response, route, failures[], guardrails[], metrics, evaluation, run_id, trace_id, session_id}`.
  `status` is one of `completed | failed | blocked | timeout | cancelled`.
  Workflow failures are returned as structured `failures`, not HTTP errors.
- `POST /chat/start`: same input; queues a background run, returns `202 {run_id, session_id, status: "queued"}`.
- `GET|POST /chat/sessions`, `GET /chat/sessions/{id}/messages`, `DELETE /chat/sessions/{id}` (all user-scoped).

## Runs and observability

- `GET /runs`, `GET /runs/{id}` (includes `events`, `steps`, `evaluation`, `metrics`, `failure`, `trace_id`)
- `GET /runs/{id}/steps`: step states (`PENDING|RUNNING|RETRYING|SUCCESS|FAILED|TIMEOUT|BLOCKED|CANCELLED`), attempts, latency, structured error
- `GET /runs/{id}/trace`: persisted spans (kind, name, latency_ms, tokens, retries, model, status)
- `GET /runs/{id}/events`, `GET /runs/{id}/logs`, `GET /runs/{id}/evaluation`
- `POST /runs/{id}/stop`: durable cancellation (`cancelled` for queued runs, `cancelling` for running ones; 409 if terminal)
- `POST /runs/{id}/retry`
- `GET /metrics/latency`: top latency contributors across the caller's recent runs

## Capabilities

- `GET /capabilities/agents`: agent specs (capabilities, tools, timeout, retry policy, token budget, permissions) plus measured activity
- `GET /capabilities/mcp`: tools with permission, risk level, required permissions, timeout, input/output schemas
- `POST /capabilities/mcp/{server_id}/test`

## Knowledge and memory

- `POST /documents/upload` (extension + magic-byte validated), `POST /documents/{id}/index|reindex`, `DELETE /documents/{id}`
- `POST /knowledge/search` `{query, top_k, document_ids}`; 503 if the embedding backend is unavailable
- `GET /memory`, `POST /memory/search`, `DELETE /memory/{id}`

## Reports, evaluations, workflows

- `GET /reports`, `GET /reports/{id}`
- `GET /evaluations`, `GET /evaluations/{id}`, `GET /evaluations/suites/latest` (offline suite report)
- `POST /workflows`, `GET /workflows`, `GET|PUT|DELETE /workflows/{id}`, `POST /workflows/{id}/run`
- `GET /runtime`: dashboard snapshot

## WebSocket events

`/ws/runs/{run_id}` replays persisted events, then streams new ones until the
run reaches a terminal status (`completed`, `failed`, `cancelled`, `blocked`,
`timeout`), then sends `stream_closed`. Event types include
`workflow_started`, `guardrail_checked`, `routing_completed`, `plan_created`,
`node_added`, `node_started`, `node_retrying`, `node_failed`,
`node_completed`, `node_blocked`, `node_timeout`, `node_cancelled`, and
`workflow_completed|failed|blocked|timeout|cancelled`.
