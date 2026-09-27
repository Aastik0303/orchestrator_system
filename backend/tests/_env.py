"""Hermetic test environment. Import this module FIRST in every test module.

* All persistent state goes to a per-process temporary directory.
* The fake LLM provider is used; no network calls are made.
* A deterministic bag-of-words hashing embedding stands in for the
  HuggingFace model so retrieval tests are meaningful and offline.
"""

from __future__ import annotations

import atexit
import hashlib
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if "ORCH_TEST_ROOT" not in os.environ:
    _root = tempfile.mkdtemp(prefix="orchestrator-tests-")
    os.environ["ORCH_TEST_ROOT"] = _root
    atexit.register(shutil.rmtree, _root, True)
TEST_ROOT = Path(os.environ["ORCH_TEST_ROOT"])

os.environ.update(
    {
        "APP_ENV": "test",
        "AUTH_MODE": "dev",
        "LLM_PROVIDER": "fake",
        "GROQ_API_KEY": "",
        "FAKE_LLM_LATENCY_MS": "0",
        "RUNTIME_DATABASE_PATH": str(TEST_ROOT / "runtime.db"),
        "STORAGE_ROOT": str(TEST_ROOT / "storage"),
        "DATABASE_URL": os.getenv("TEST_DATABASE_URL", ""),
        "REDIS_URL": "",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "MAX_ACTIVE_RUNS_PER_USER": "1000",
        "LOG_LEVEL": "CRITICAL",
        "WORKER_MODE": "inprocess",
        "ROUTER_LLM_ENABLED": "true",
        # Network adapters are off unless a test enables them with a mock.
        "WEB_FETCH_ENABLED": "false",
        "WEB_SEARCH_API_KEY": "",
        "GITHUB_ENABLED": "false",
        "GITHUB_TOKEN": "",
        "YOUTUBE_TRANSCRIPTS_ENABLED": "false",
        "SQL_AGENT_DATABASE_URL": "",
    }
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
STOP = {"the", "and", "for", "with", "what", "which", "that", "this", "does", "from", "are", "was", "how", "who"}


class HashEmbedding:
    """Deterministic bag-of-words embedding (LangChain Embeddings interface)."""

    def __init__(self, size: int = 384) -> None:
        self.size = size

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.size
        for token in TOKEN_RE.findall(text.lower()):
            if token in STOP:
                continue
            token = token[:-1] if token.endswith("s") and len(token) > 4 else token
            index = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.size
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


def use_hash_embeddings():
    """Patch the embedding backend; returns the started patcher."""
    from unittest.mock import patch

    from app.rag.service import embedding_service

    patcher = patch.object(embedding_service, "_get_backend", return_value=HashEmbedding())
    patcher.start()
    return patcher


def unique(prefix: str) -> str:
    return f"{prefix}-{os.urandom(4).hex()}"


import logging  # noqa: E402

# Expected warnings (e.g. embedding backend absent) are exercised deliberately.
logging.disable(logging.CRITICAL)

# Hermetic by default: never load the real embedding model in unit tests (it
# is slow and machine-dependent). Tests that need it opt in explicitly.
if os.getenv("TEST_REAL_EMBEDDINGS") != "1":
    _default_embedding_patch = use_hash_embeddings()
