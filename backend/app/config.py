"""Application settings.

Values are read from the environment when `Settings()` is instantiated (not at
import time), so tests and workers can override them and call
`get_settings.cache_clear()`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env")


def _str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value is not None else default


def _float(name: str, default: float) -> float:
    value = _str(name)
    return float(value) if value is not None else default


def _env(factory):
    return Field(default_factory=factory)


class Settings(BaseModel):
    # Runtime environment
    app_env: str = _env(lambda: _str("APP_ENV", "development"))
    log_level: str = _env(lambda: _str("LOG_LEVEL", "INFO"))
    cors_origins: list[str] = _env(
        lambda: [
            origin.strip()
            for origin in (
                _str("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173") or ""
            ).split(",")
            if origin.strip()
        ]
    )

    # Authentication: "dev" trusts a caller-supplied user id (local only);
    # "api_key" derives the user from a hashed API key.
    auth_mode: str = _env(lambda: _str("AUTH_MODE", "dev"))
    api_keys: str | None = _env(lambda: _str("API_KEYS"))

    # Model provider
    llm_provider: str = _env(lambda: _str("LLM_PROVIDER", "groq"))
    groq_api_key: str | None = _env(lambda: _str("GROQ_API_KEY"))
    groq_model: str = _env(lambda: _str("GROQ_MODEL", "llama-3.3-70b-versatile"))
    groq_model_fast: str = _env(lambda: _str("GROQ_MODEL_FAST", "llama-3.1-8b-instant"))
    llm_request_timeout_seconds: float = _env(lambda: _float("LLM_REQUEST_TIMEOUT_SECONDS", 20))
    llm_max_output_tokens: int = _env(lambda: _int("LLM_MAX_OUTPUT_TOKENS", 1200))
    llm_cache_size: int = _env(lambda: _int("LLM_CACHE_SIZE", 256))
    fake_llm_latency_ms: int = _env(lambda: _int("FAKE_LLM_LATENCY_MS", 0))

    # Persistence and infrastructure
    database_url: str | None = _env(lambda: _str("DATABASE_URL"))
    runtime_database_path: str = _env(lambda: _str("RUNTIME_DATABASE_PATH", "storage/runtime.db"))
    database_pool_size: int = _env(lambda: _int("DATABASE_POOL_SIZE", 10))
    database_pool_pre_ping: bool = _env(lambda: _bool("DATABASE_POOL_PRE_PING", True))
    database_pool_recycle_seconds: int = _env(lambda: _int("DATABASE_POOL_RECYCLE_SECONDS", 1800))
    redis_url: str | None = _env(lambda: _str("REDIS_URL"))
    storage_root: str = _env(lambda: _str("STORAGE_ROOT", "storage"))
    worker_mode: str = _env(lambda: _str("WORKER_MODE", "inprocess"))
    worker_poll_seconds: float = _env(lambda: _float("WORKER_POLL_SECONDS", 0.5))

    # Retrieval
    embedding_provider: str = _env(lambda: _str("EMBEDDING_PROVIDER", "huggingface"))
    embedding_model: str = _env(
        lambda: _str("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    )
    embedding_dimensions: int = _env(lambda: _int("EMBEDDING_DIMENSIONS", 384))
    embedding_device: str = _env(lambda: _str("EMBEDDING_DEVICE", "cpu"))
    embedding_batch_size: int = _env(lambda: _int("EMBEDDING_BATCH_SIZE", 32))
    embedding_local_files_only: bool = _env(lambda: _bool("EMBEDDING_LOCAL_FILES_ONLY", True))
    # torch intra-op threads for embedding calls (0 = torch default).
    embedding_torch_threads: int = _env(lambda: _int("EMBEDDING_TORCH_THREADS", 1))
    rag_chunk_size: int = _env(lambda: _int("RAG_CHUNK_SIZE", 900))
    rag_chunk_overlap: int = _env(lambda: _int("RAG_CHUNK_OVERLAP", 140))
    rag_top_k: int = _env(lambda: _int("RAG_TOP_K", 5))
    # auto: pgvector (HNSW index, search in the database) when running on
    # PostgreSQL with the extension available; NumPy otherwise.
    vector_backend: str = _env(lambda: _str("VECTOR_BACKEND", "auto"))
    rag_similarity_threshold: float = _env(lambda: _float("RAG_SIMILARITY_THRESHOLD", 0.30))
    # Drop chunks scoring below this fraction of the best match (keeps weakly
    # related chunks out of the context even when above the absolute floor).
    rag_relative_score_cutoff: float = _env(lambda: _float("RAG_RELATIVE_SCORE_CUTOFF", 0.5))
    rag_max_context_chars: int = _env(lambda: _int("RAG_MAX_CONTEXT_CHARS", 6000))
    rag_max_chunks_per_document: int = _env(lambda: _int("RAG_MAX_CHUNKS_PER_DOCUMENT", 3))

    # Routing and planning
    router_confidence_threshold: float = _env(lambda: _float("ROUTER_CONFIDENCE_THRESHOLD", 0.65))
    router_llm_enabled: bool = _env(lambda: _bool("ROUTER_LLM_ENABLED", True))
    general_chat_enabled: bool = _env(lambda: _bool("GENERAL_CHAT_ENABLED", True))
    evaluation_passing_score: float = _env(lambda: _float("EVALUATION_PASSING_SCORE", 75))

    # Workflow budgets
    max_agent_retries: int = _env(lambda: _int("MAX_AGENT_RETRIES", 2))
    max_tool_retries: int = _env(lambda: _int("MAX_TOOL_RETRIES", 2))
    max_plan_nodes: int = _env(lambda: _int("MAX_PLAN_NODES", 20))
    max_plan_depth: int = _env(lambda: _int("MAX_PLAN_DEPTH", 8))
    max_execution_seconds: int = _env(lambda: _int("MAX_EXECUTION_SECONDS", 180))
    max_tool_calls: int = _env(lambda: _int("MAX_TOOL_CALLS", 25))
    max_tokens_per_run: int = _env(lambda: _int("MAX_TOKENS_PER_RUN", 60000))
    max_retries_per_run: int = _env(lambda: _int("MAX_RETRIES_PER_RUN", 6))
    max_llm_calls_per_run: int = _env(lambda: _int("MAX_LLM_CALLS_PER_RUN", 12))
    max_parallel_steps: int = _env(lambda: _int("MAX_PARALLEL_STEPS", 4))
    step_input_max_chars: int = _env(lambda: _int("STEP_INPUT_MAX_CHARS", 4000))

    # Guardrails and abuse prevention
    guardrail_mode: str = _env(lambda: _str("GUARDRAIL_MODE", "enforce"))
    pii_redaction: str = _env(lambda: _str("PII_REDACTION", "standard"))
    max_message_chars: int = _env(lambda: _int("MAX_MESSAGE_CHARS", 20000))
    rate_limit_per_minute: int = _env(lambda: _int("RATE_LIMIT_PER_MINUTE", 30))
    max_active_runs_per_user: int = _env(lambda: _int("MAX_ACTIVE_RUNS_PER_USER", 3))

    # Sandboxed code execution
    sandbox_enabled: bool = _env(lambda: _bool("SANDBOX_ENABLED", True))
    sandbox_timeout_seconds: float = _env(lambda: _float("SANDBOX_TIMEOUT_SECONDS", 5))
    sandbox_memory_mb: int = _env(lambda: _int("SANDBOX_MEMORY_MB", 256))
    sandbox_max_output_bytes: int = _env(lambda: _int("SANDBOX_MAX_OUTPUT_BYTES", 20000))

    # Uploads
    max_upload_size: int = _env(lambda: _int("MAX_UPLOAD_SIZE", 20 * 1024 * 1024))
    max_files_per_request: int = _env(lambda: _int("MAX_FILES_PER_REQUEST", 5))
    allowed_upload_extensions: set[str] = Field(
        default_factory=lambda: {".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".xls"}
    )

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{Path(self.runtime_database_path).as_posix()}"

    @property
    def uploads_dir(self) -> Path:
        return Path(self.storage_root) / "uploads"

    @property
    def outputs_dir(self) -> Path:
        return Path(self.storage_root) / "outputs"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    def secret_values(self) -> list[str]:
        """Secrets that must never appear in responses, logs, or events."""
        values = [self.groq_api_key, self.redis_url, self.api_keys]
        for name in ("GITHUB_TOKEN", "WEB_SEARCH_API_KEY", "POSTGRES_PASSWORD"):
            values.append(os.getenv(name))
        if self.database_url and "@" in self.database_url:
            values.append(self.database_url)
        return [value for value in values if value and len(value) >= 6]


@lru_cache
def get_settings() -> Settings:
    return Settings()
