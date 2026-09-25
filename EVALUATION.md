# Evaluation

Two layers:

1. **Offline suites** (`backend/evals/`): router, RAG, agent, security and
   performance, with thresholds. Run hermetically (temporary storage, fake LLM
   with scripted answers where a suite needs them, a lexical embedding proxy).
2. **Online critic**: every planned run ends with the `evaluation` agent, which
   scores the final report (correctness, relevance, completeness,
   groundedness, hallucination risk, tool success, format) and persists the
   result (`/api/runs/{id}/evaluation`).

## Running

```powershell
cd backend
..\.venv\Scripts\python.exe -m evals.run                   # all suites
..\.venv\Scripts\python.exe -m evals.run --suite security  # one suite
```

Results: `backend/evals/results/latest.json` (also served at
`GET /api/evaluations/suites/latest` and shown on the Evaluations page). The
process exits non-zero if a threshold fails; `tests/test_evals.py` runs the
router/RAG/agent/security suites as part of the unit test run.

## Suites and latest results

Recorded 2026-09-25 on the development machine (Windows 11, Python 3.13).

| Suite | Metric | Threshold | Result |
|---|---|---|---|
| Router (24 cases) | accuracy (expected vs actual capability, multi-intent sets) | ≥ 0.90 | **1.00** |
| RAG (6 cases, 5 docs incl. poisoned + secret-bearing) | retrieval hit rate | ≥ 0.90 | **1.00** |
| | mean context relevance | reported | **0.917** |
| | faithfulness detection (unsupported answers flagged, supported not) | ≥ 0.80 | **1.00** |
| | injection text reaching the model | 0 | **0** |
| | secret leaks (prompt or answer) | 0 | **0** |
| Agent (6 scenarios) | task success | 1.00 | **1.00** |
| | tool correctness (approval gating, sandbox blocking) | 1.00 | **1.00** |
| Security (24 attacks, 8 categories) | block rate | 1.00 | **1.00** |
| | leaks (model scripted to leak secret + canary) | 0 | **0** |
| | model calls on attacks | reported | **0** |
| | benign false-positive rate (12 benign prompts) | ≤ 0.10 | **0.00** |
| Performance (in-process, 50 ms simulated LLM) | error rate at c=1/10/25 | 0 | **0** |

Performance suite detail (orchestrator only, no HTTP): p50 74.5 / 221.9 /
437.1 ms and 13.3 / 34.4 / 35.4 runs/s at concurrency 1 / 10 / 25, ~545 tokens
per run (fake provider token estimates). End-to-end HTTP numbers are in
[PERFORMANCE.md](PERFORMANCE.md).

### What each suite checks

- **Router**: input, expected capability, actual capability, agent,
  confidence, strategy, correct/incorrect. Includes file-attachment rules and
  multi-intent requests.
- **RAG**: question, retrieved documents (parsed from the actual prompt sent
  to the model), context relevance (share of retrieved documents that are the
  relevant one), retrieval hit, whether the answer was flagged unsupported,
  injection and secret leakage into the prompt/answer. Includes irrelevant and
  unsupported questions and a hallucinated answer.
- **Agent**: dataset profiling, approved sandbox execution, multi-agent report
  with critic score, conversation; tool correctness (sandbox approval gate,
  malicious code blocked).
- **Security**: prompt injection, jailbreak, credential extraction, system
  prompt extraction, PII / cross-user access, tool abuse, malicious and
  resource-abuse requests. The model is scripted to leak the configured secret
  and canary, so any bypass would show up as a leak.
- **Performance**: latency percentiles, token usage and error rate under
  concurrency.

## RAG with the real embedding model

`EVAL_REAL_EMBEDDINGS=1 python -m evals.run` runs the suites with the
configured model (`sentence-transformers/all-MiniLM-L6-v2`, CPU) instead of the
lexical proxy; the report is also saved as
`evals/results/latest-real-embeddings.json`.

| Metric | Lexical proxy | Real model, threshold 0.12 (old default) | Real model, threshold 0.30 (new default) |
|---|---|---|---|
| retrieval hit rate | 1.00 | 0.833 | **1.00** |
| mean context relevance | 0.917 | 0.833 | **1.00** |
| faithfulness detection | 1.00 | 1.00 | **1.00** |
| injection / secret leaks | 0 / 0 | 0 / 0 | **0 / 0** |

**Threshold calibration.** With the real model, the old similarity floor
(0.12) let an unrelated document into the context for "What is the capital of
Mongolia?". Measured cosine similarities over the corpus and 12 questions:
relevant question/document pairs scored ≥ 0.42 (median 0.68); unrelated pairs
scored ≤ 0.18. The two "irrelevant" pairs above that (0.39, 0.46) were the
poisoned refund addendum and the secret-bearing onboarding runbook, which are
topically related and handled by the retrieval guardrail. `RAG_SIMILARITY_THRESHOLD`
now defaults to **0.30**, which separates the classes; re-calibrate it if you
change `EMBEDDING_MODEL`.

## Honest limitations

- By default (and in `tests/test_evals.py`) the RAG suite uses a lexical hashing
  embedding (stopwords removed, naive stemming) so it is fast and hermetic; it
  measures the pipeline (filtering, guards, context building,
  citation/faithfulness checks). Use `EVAL_REAL_EMBEDDINGS=1` for the model's
  retrieval quality (table above). The 6-question corpus is small: treat the
  calibrated threshold as a starting point, not a universal constant.
- Faithfulness is a lexical-overlap signal, not an NLI or LLM judge.
- Answers are scripted, so the suites test the platform's handling of model
  behavior, not a real model's quality. With `LLM_PROVIDER=groq` the agent
  suite exercises real generations (costs apply).
- The datasets are small and hand-written; extend `evals/datasets.py` when
  adding capabilities or fixing a misroute (add the failing case first).
