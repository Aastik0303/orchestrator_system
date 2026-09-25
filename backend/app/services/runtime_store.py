"""Durable runtime repository (SQLAlchemy Core).

One repository serves both deployment targets:

* SQLite (local development, default): WAL mode, pooled connections, pragmas
  applied once per connection instead of once per query.
* PostgreSQL (`DATABASE_URL=postgresql+psycopg://...`): the same Core
  statements, pooled with pre-ping. This is the target for horizontally scaled
  API replicas and workers, because every piece of workflow state (runs, steps,
  events, spans, queue, cancellation flags) lives here instead of in process
  memory.

All methods are synchronous; async callers must use `asyncio.to_thread` so the
event loop is never blocked by database I/O.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import uuid4

import numpy as np
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.engine import Connection, Engine

from app.config import get_settings
from app.observability.telemetry import maybe_span

F = TypeVar("F", bound=Callable[..., Any])
logger = logging.getLogger("orchestrator.store")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _vector_blob(vector: list[float] | np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _vector_from_row(blob: bytes | None, legacy_json: str | None) -> np.ndarray | None:
    if blob:
        return np.frombuffer(blob, dtype=np.float32)
    values = _loads(legacy_json, None)
    if values:
        return np.asarray(values, dtype=np.float32)
    return None


metadata = sa.MetaData()

workflow_runs = sa.Table(
    "workflow_runs",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("session_id", sa.String(64)),
    sa.Column("trace_id", sa.String(64)),
    sa.Column("task", sa.Text, nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("active_agent", sa.String(64)),
    sa.Column("route_json", sa.Text),
    sa.Column("plan_json", sa.Text),
    sa.Column("response", sa.Text, nullable=False, server_default=""),
    sa.Column("artifacts_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("needs_clarification", sa.Integer, nullable=False, server_default="0"),
    sa.Column("file_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text),
    sa.Column("error_json", sa.Text),
    sa.Column("metrics_json", sa.Text),
    sa.Column("guardrails_json", sa.Text),
    sa.Column("request_json", sa.Text),
    sa.Column("worker_id", sa.String(64)),
    sa.Column("cancel_requested", sa.Integer, nullable=False, server_default="0"),
    sa.Column("started_at", sa.String(40), nullable=False),
    sa.Column("completed_at", sa.String(40)),
    sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
    sa.Index("idx_workflow_runs_owner", "user_id", "project_id", "started_at"),
    sa.Index("idx_workflow_runs_status", "status", "started_at"),
)

execution_events = sa.Table(
    "execution_events",
    metadata,
    sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("id", sa.String(64), nullable=False, unique=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
    sa.Column("event_type", sa.String(64), nullable=False),
    sa.Column("node_id", sa.String(128)),
    sa.Column("parent_node_id", sa.String(128)),
    sa.Column("node_type", sa.String(32)),
    sa.Column("label", sa.String(160)),
    sa.Column("status", sa.String(32)),
    sa.Column("details_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Index("idx_execution_events_run", "run_id", "sequence"),
)

run_steps = sa.Table(
    "run_steps",
    metadata,
    sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("step_id", sa.String(128), primary_key=True),
    sa.Column("agent", sa.String(64)),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("depends_on_json", sa.Text, nullable=False, server_default="[]"),
    sa.Column("error_json", sa.Text),
    sa.Column("output_preview", sa.Text),
    sa.Column("latency_ms", sa.Integer),
    sa.Column("started_at", sa.String(40)),
    sa.Column("completed_at", sa.String(40)),
)

run_spans = sa.Table(
    "run_spans",
    metadata,
    sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("span_id", sa.String(32), primary_key=True),
    sa.Column("parent_id", sa.String(32)),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("name", sa.String(160), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("latency_ms", sa.Float, nullable=False),
    sa.Column("attributes_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("started_at", sa.String(40), nullable=False),
    sa.Index("idx_run_spans_kind", "kind", "name"),
)

documents = sa.Table(
    "documents",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("content_type", sa.String(160)),
    sa.Column("storage_path", sa.Text),
    sa.Column("size", sa.Integer, nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("embedding_model", sa.String(255)),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.Index("idx_documents_owner", "user_id", "project_id", "created_at"),
)

document_chunks = sa.Table(
    "document_chunks",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("document_id", sa.String(64), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    sa.Column("chunk_index", sa.Integer, nullable=False),
    sa.Column("page_number", sa.Integer),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("embedding_json", sa.Text, nullable=False, server_default=""),
    sa.Column("embedding_blob", sa.LargeBinary),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.UniqueConstraint("document_id", "chunk_index"),
    sa.Index("idx_document_chunks_document", "document_id", "chunk_index"),
)

memories = sa.Table(
    "memories",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("project_id", sa.String(128)),
    sa.Column("memory_type", sa.String(32), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("importance_score", sa.Float, nullable=False),
    sa.Column("source_session_id", sa.String(64)),
    sa.Column("source_run_id", sa.String(64)),
    sa.Column("embedding_json", sa.Text, nullable=False, server_default=""),
    sa.Column("embedding_blob", sa.LargeBinary),
    sa.Column("metadata_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Index("idx_memories_owner", "user_id", "project_id", "created_at"),
)

reports = sa.Table(
    "reports",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("title", sa.String(255), nullable=False),
    sa.Column("format", sa.String(32), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
)

evaluations = sa.Table(
    "evaluations",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("result_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
)

workflows = sa.Table(
    "workflows",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("name", sa.String(160), nullable=False),
    sa.Column("definition_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("updated_at", sa.String(40), nullable=False),
)

chat_sessions = sa.Table(
    "chat_sessions",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(128), nullable=False, server_default="local-user"),
    sa.Column("title", sa.String(160), nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("updated_at", sa.String(40), nullable=False),
    sa.Index("idx_chat_sessions_owner", "user_id", "updated_at"),
)

chat_messages = sa.Table(
    "chat_messages",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("session_id", sa.String(64), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
    sa.Column("role", sa.String(32), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("attachment_name", sa.String(255)),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Index("idx_chat_messages_session", "session_id", "created_at"),
)

# Columns added after the first SQLite release; applied with ALTER TABLE on
# existing local databases (create_all never alters tables).
_LATE_COLUMNS: dict[str, dict[str, str]] = {
    "workflow_runs": {
        "trace_id": "VARCHAR(64)",
        "error_json": "TEXT",
        "metrics_json": "TEXT",
        "guardrails_json": "TEXT",
        "request_json": "TEXT",
        "worker_id": "VARCHAR(64)",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
    },
    "documents": {"embedding_model": "VARCHAR(255)"},
    "document_chunks": {"embedding_blob": "BLOB"},
    "memories": {"embedding_blob": "BLOB"},
    "chat_sessions": {"user_id": "VARCHAR(128) NOT NULL DEFAULT 'local-user'"},
}

ACTIVE_RUN_STATUSES = ("queued", "running")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "blocked", "timeout")

_ENGINES: dict[str, Engine] = {}
_ENGINES_LOCK = threading.Lock()


def _engine_for(url: str) -> Engine:
    """Process-wide engine (connection pool) cache keyed by database URL."""
    with _ENGINES_LOCK:
        engine = _ENGINES.get(url)
        if engine is not None:
            return engine
        settings = get_settings()
        if url.startswith("sqlite"):
            database_path = url.split("///", 1)[-1]
            if database_path and database_path != ":memory:":
                Path(database_path).parent.mkdir(parents=True, exist_ok=True)
            engine = sa.create_engine(
                url,
                connect_args={"check_same_thread": False, "timeout": 30},
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_pool_size,
                pool_pre_ping=False,
            )

            @sa.event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode = WAL")
                # NORMAL is durable under WAL except for the last transactions
                # on power loss; acceptable for the local adapter.
                cursor.execute("PRAGMA synchronous = NORMAL")
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA busy_timeout = 30000")
                cursor.close()
        else:
            engine = sa.create_engine(
                url,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_pool_size,
                # Pre-ping costs one round trip per checkout; stale connections
                # are also handled by recycling and SQLAlchemy's disconnect
                # invalidation, so it is configurable.
                pool_pre_ping=settings.database_pool_pre_ping,
                pool_recycle=settings.database_pool_recycle_seconds,
            )
        _ENGINES[url] = engine
        return engine


def dispose_engines() -> None:
    with _ENGINES_LOCK:
        for engine in _ENGINES.values():
            engine.dispose()
        _ENGINES.clear()


def _columns(engine: Engine, table: str) -> set[str]:
    # The pgvector column type is unknown to SQLAlchemy's reflection; only
    # column names are needed here, so silence that warning.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Did not recognize type")
        return {column["name"] for column in sa.inspect(engine).get_columns(table)}


def _instrumented(method: F) -> F:
    """Record a `db` span for each repository call when a run is being traced."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with maybe_span("db", method.__name__):
            return method(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


class RuntimeStore:
    def __init__(self, database: str | Path | None = None) -> None:
        settings = get_settings()
        if database is None:
            self.url = settings.resolved_database_url
        elif isinstance(database, Path) or "://" not in str(database):
            self.url = f"sqlite:///{Path(database).as_posix()}"
        else:
            self.url = str(database)
        self._initialized = False
        self._init_lock = threading.Lock()
        self.vector_enabled = False
        self._vector_schema = "public"

    @property
    def engine(self) -> Engine:
        return _engine_for(self.url)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @property
    def database_path(self) -> Path | None:
        if self.url.startswith("sqlite"):
            return Path(self.url.split("///", 1)[-1])
        return None

    def close(self) -> None:
        """Dispose this store's connection pool (releases SQLite file handles)."""
        with _ENGINES_LOCK:
            engine = _ENGINES.pop(self.url, None)
        if engine is not None:
            engine.dispose()
        self._initialized = False

    def begin(self):
        """Transactional connection context manager."""
        self.initialize()
        return self.engine.begin()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            engine = self.engine
            metadata.create_all(engine)
            self._apply_late_columns(engine)
            self.vector_enabled = self._setup_pgvector(engine)
            self._initialized = True

    def _setup_pgvector(self, engine: Engine) -> bool:
        """Enable in-database vector search (pgvector + HNSW) on PostgreSQL."""
        settings = get_settings()
        backend = settings.vector_backend.lower()
        if engine.dialect.name != "postgresql" or backend == "numpy":
            return False
        try:
            with engine.begin() as connection:
                connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            with engine.connect() as connection:
                installed = connection.execute(
                    sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).first()
            if not installed:
                if backend == "pgvector":
                    raise RuntimeError("VECTOR_BACKEND=pgvector but the vector extension is unavailable.") from exc
                logger.warning("pgvector_unavailable_using_numpy")
                return False
        # The extension may live in any schema (it is installed into the first
        # schema on the search path); qualify every reference with it.
        with engine.connect() as connection:
            self._vector_schema = connection.execute(
                sa.text(
                    "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
                    "WHERE e.extname = 'vector'"
                )
            ).scalar_one()
        vector_type = self._vector_type
        dims = settings.embedding_dimensions
        columns = _columns(engine, "document_chunks")
        with engine.begin() as connection:
            if "embedding_vec" not in columns:
                connection.execute(
                    sa.text(f"ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_vec {vector_type}({dims})")
                )
                # One-time backfill from the portable float32 blobs.
                rows = connection.execute(
                    sa.select(document_chunks.c.id, document_chunks.c.embedding_blob, document_chunks.c.embedding_json)
                ).all()
                updates = []
                for chunk_id, blob, legacy in rows:
                    vector = _vector_from_row(blob, legacy)
                    if vector is not None and vector.shape[0] == dims:
                        updates.append({"id": chunk_id, "v": _vector_literal(vector)})
                if updates:
                    connection.execute(self._vector_update_statement(), updates)
            connection.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding_hnsw "
                    f'ON document_chunks USING hnsw (embedding_vec "{self._vector_schema}".vector_cosine_ops)'
                )
            )
        return True

    @property
    def _vector_type(self) -> str:
        return f'"{self._vector_schema}".vector'

    def _vector_update_statement(self):
        return sa.text(f"UPDATE document_chunks SET embedding_vec = CAST(:v AS {self._vector_type}) WHERE id = :id")

    def _apply_late_columns(self, engine: Engine) -> None:
        with engine.begin() as connection:
            for table_name, columns in _LATE_COLUMNS.items():
                existing = _columns(engine, table_name)
                for column_name, ddl in columns.items():
                    if column_name in existing:
                        continue
                    if engine.dialect.name == "postgresql":
                        ddl = ddl.replace("BLOB", "BYTEA")
                    connection.execute(
                        sa.text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")
                    )
        # create_all only builds indexes for tables it creates; add indexes
        # introduced later to databases that predate them.
        for table in metadata.sorted_tables:
            for index in table.indexes:
                index.create(engine, checkfirst=True)

    def _upsert(
        self,
        connection: Connection,
        table: sa.Table,
        values: dict[str, Any],
        *,
        conflict_columns: list[str],
        update_columns: list[str] | None,
    ) -> None:
        insert = (pg_dialect.insert if self.dialect == "postgresql" else sqlite_dialect.insert)(table)
        statement = insert.values(**values)
        if update_columns:
            statement = statement.on_conflict_do_update(
                index_elements=conflict_columns,
                set_={column: statement.excluded[column] for column in update_columns},
            )
        else:
            statement = statement.on_conflict_do_nothing(index_elements=conflict_columns)
        connection.execute(statement)

    # ------------------------------------------------------------------ runs

    @_instrumented
    def create_run(
        self,
        *,
        run_id: str,
        user_id: str,
        project_id: str,
        session_id: str | None,
        task: str,
        file_count: int,
        trace_id: str | None = None,
        request_json: str | None = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        with self.begin() as connection:
            connection.execute(
                workflow_runs.insert().values(
                    id=run_id,
                    user_id=user_id,
                    project_id=project_id,
                    session_id=session_id,
                    trace_id=trace_id,
                    task=task,
                    status=status,
                    file_count=file_count,
                    request_json=request_json,
                    started_at=utc_now(),
                    response="",
                    artifacts_json="[]",
                    needs_clarification=0,
                    cancel_requested=0,
                    duration_ms=0,
                )
            )
        return self.get_run(run_id, user_id=user_id) or {}

    RUN_UPDATE_COLUMNS = {
        "status",
        "active_agent",
        "route_json",
        "plan_json",
        "response",
        "artifacts_json",
        "needs_clarification",
        "error",
        "error_json",
        "metrics_json",
        "guardrails_json",
        "completed_at",
        "duration_ms",
        "trace_id",
        "worker_id",
    }

    @_instrumented
    def start_run(
        self,
        *,
        run_id: str,
        user_id: str,
        project_id: str,
        session_id: str | None,
        task: str,
        file_count: int,
        trace_id: str | None,
    ) -> str | None:
        """Create the run as `running`, or mark an existing queued run as
        running, in ONE statement (upsert with a guarded update + RETURNING;
        minimizes round trips on networked databases). Returns None if the run
        may proceed, or the status of an existing run that must not execute
        (e.g. cancelled while queued)."""
        insert = (pg_dialect.insert if self.dialect == "postgresql" else sqlite_dialect.insert)(workflow_runs)
        statement = (
            insert.values(
                id=run_id,
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                trace_id=trace_id,
                task=task,
                status="running",
                file_count=file_count,
                started_at=utc_now(),
                response="",
                artifacts_json="[]",
                needs_clarification=0,
                cancel_requested=0,
                duration_ms=0,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"status": "running", "trace_id": insert.excluded.trace_id},
                where=sa.and_(
                    workflow_runs.c.user_id == user_id,
                    workflow_runs.c.status.notin_(TERMINAL_RUN_STATUSES),
                    workflow_runs.c.cancel_requested == 0,
                ),
            )
            .returning(workflow_runs.c.id)
        )
        with self.begin() as connection:
            if connection.execute(statement).first() is not None:
                return None
            existing = connection.execute(
                sa.select(workflow_runs.c.status).where(workflow_runs.c.id == run_id)
            ).first()
        if existing is None:  # pragma: no cover - defensive
            return "failed"
        return existing[0] if existing[0] in TERMINAL_RUN_STATUSES else "cancelled"

    @_instrumented
    def apply_batch(self, run_id: str, operations: list[tuple[str, Any]]) -> None:
        """Apply queued events, step upserts and run updates in ONE transaction.

        Events keep their order (one executemany); step updates are merged per
        step in order (later values win) and upserted with one compiled
        statement per column set; run updates are merged into one UPDATE.
        Building a statement per row was the dominant CPU cost per run.
        """
        if not operations:
            return
        events: list[dict[str, Any]] = []
        steps: dict[str, dict[str, Any]] = {}
        run_updates: dict[str, Any] = {}
        now = utc_now()
        for kind, payload in operations:
            if kind == "event":
                events.append(
                    {
                        "id": f"evt_{uuid4().hex[:16]}",
                        "run_id": run_id,
                        "event_type": payload["type"],
                        "node_id": payload.get("node_id"),
                        "parent_node_id": payload.get("parent_node_id"),
                        "node_type": payload.get("node_type"),
                        "label": payload.get("label"),
                        "status": payload.get("status"),
                        "details_json": _json(payload.get("details") or {}),
                        "created_at": now,
                    }
                )
            elif kind == "step":
                step_id, values = payload
                merged = steps.setdefault(step_id, {})
                merged.update({key: value for key, value in values.items() if key in self.STEP_COLUMNS})
            elif kind == "run":
                run_updates.update({key: value for key, value in payload.items() if key in self.RUN_UPDATE_COLUMNS})
            else:
                raise ValueError(f"Unknown batch operation: {kind}")

        with self.begin() as connection:
            if events:
                connection.execute(execution_events.insert(), events)
            by_columns: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for step_id, values in steps.items():
                values.setdefault("status", "PENDING")
                columns = tuple(sorted(values))
                by_columns.setdefault(columns, []).append({"run_id": run_id, "step_id": step_id, **values})
            for columns, rows in by_columns.items():
                connection.execute(self._step_upsert_statement(columns), rows)
            if run_updates:
                connection.execute(workflow_runs.update().where(workflow_runs.c.id == run_id).values(**run_updates))

    def _step_upsert_statement(self, columns: tuple[str, ...]):
        cache = self.__dict__.setdefault("_step_upserts", {})
        statement = cache.get(columns)
        if statement is None:
            insert = (pg_dialect.insert if self.dialect == "postgresql" else sqlite_dialect.insert)(run_steps)
            statement = insert.on_conflict_do_update(
                index_elements=["run_id", "step_id"],
                set_={column: insert.excluded[column] for column in columns},
            )
            cache[columns] = statement
        return statement

    @_instrumented
    def update_run(self, run_id: str, **values: Any) -> None:
        updates = {key: value for key, value in values.items() if key in self.RUN_UPDATE_COLUMNS}
        if not updates:
            return
        with self.begin() as connection:
            connection.execute(
                workflow_runs.update().where(workflow_runs.c.id == run_id).values(**updates)
            )

    @_instrumented
    def finish_run(self, run_id: str, *, status: str, **values: Any) -> bool:
        """Move a run to a terminal status unless it is already terminal or a
        cancellation was requested (e.g. by another replica) - a cancel always
        wins over a late completion. Returns True if this call set the status."""
        return self.complete_run(run_id, status=status, **values)

    @_instrumented
    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        report: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
        **values: Any,
    ) -> bool:
        """Finish a run and persist its report/evaluation in ONE transaction."""
        updates = {key: value for key, value in values.items() if key in self.RUN_UPDATE_COLUMNS}
        updates["status"] = status
        statement = (
            workflow_runs.update()
            .where(workflow_runs.c.id == run_id)
            .where(workflow_runs.c.status.notin_(TERMINAL_RUN_STATUSES))
        )
        if status != "cancelled":
            statement = statement.where(workflow_runs.c.cancel_requested == 0)
        now = utc_now()
        with self.begin() as connection:
            if evaluation is not None:
                self._upsert(
                    connection,
                    evaluations,
                    {
                        "id": f"eval_{uuid4().hex[:16]}",
                        "run_id": run_id,
                        "user_id": evaluation["user_id"],
                        "project_id": evaluation["project_id"],
                        "result_json": _json(evaluation["result"]),
                        "created_at": now,
                    },
                    conflict_columns=["run_id"],
                    update_columns=["result_json", "created_at"],
                )
            if report is not None:
                self._upsert(
                    connection,
                    reports,
                    {
                        "id": f"report_{uuid4().hex[:16]}",
                        "run_id": run_id,
                        "user_id": report["user_id"],
                        "project_id": report["project_id"],
                        "title": report["title"][:255],
                        "format": report.get("format", "markdown"),
                        "content": report["content"],
                        "created_at": now,
                    },
                    conflict_columns=["run_id"],
                    update_columns=["title", "format", "content", "created_at"],
                )
            result = connection.execute(statement.values(**updates))
            if result.rowcount == 0 and status != "cancelled":
                # Cancellation requested but the run is not terminal yet:
                # record the result under the cancelled status.
                connection.execute(
                    workflow_runs.update()
                    .where(workflow_runs.c.id == run_id)
                    .where(workflow_runs.c.status.notin_(TERMINAL_RUN_STATUSES))
                    .values(**{**updates, "status": "cancelled"})
                )
        return result.rowcount > 0

    @_instrumented
    def list_runs(
        self,
        *,
        user_id: str = "local-user",
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        statement = sa.select(workflow_runs).where(workflow_runs.c.user_id == user_id)
        if project_id:
            statement = statement.where(workflow_runs.c.project_id == project_id)
        statement = statement.order_by(workflow_runs.c.started_at.desc()).limit(limit)
        with self.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._run_row(row) for row in rows]

    @_instrumented
    def get_run(self, run_id: str, *, user_id: str = "local-user") -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(workflow_runs).where(
                        workflow_runs.c.id == run_id, workflow_runs.c.user_id == user_id
                    )
                )
                .mappings()
                .first()
            )
        return self._run_row(row) if row else None

    @_instrumented
    def get_run_status(self, run_id: str) -> tuple[str | None, bool]:
        """Cheap poll used for cross-replica cancellation."""
        with self.begin() as connection:
            row = connection.execute(
                sa.select(workflow_runs.c.status, workflow_runs.c.cancel_requested).where(
                    workflow_runs.c.id == run_id
                )
            ).first()
        if not row:
            return None, False
        return row[0], bool(row[1])

    @_instrumented
    def request_cancel(self, run_id: str, *, user_id: str) -> str | None:
        """Flag a run for cancellation. Queued runs are cancelled immediately;
        running runs are stopped by whichever process executes them."""
        with self.begin() as connection:
            row = connection.execute(
                sa.select(workflow_runs.c.status).where(
                    workflow_runs.c.id == run_id, workflow_runs.c.user_id == user_id
                )
            ).first()
            if not row:
                return None
            status = row[0]
            if status in TERMINAL_RUN_STATUSES:
                return status
            values: dict[str, Any] = {"cancel_requested": 1}
            if status == "queued":
                values.update(status="cancelled", completed_at=utc_now())
            connection.execute(
                workflow_runs.update().where(workflow_runs.c.id == run_id).values(**values)
            )
        return "cancelled" if status == "queued" else "cancelling"

    @_instrumented
    def count_active_runs(self, *, user_id: str) -> int:
        with self.begin() as connection:
            return int(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(workflow_runs)
                    .where(
                        workflow_runs.c.user_id == user_id,
                        workflow_runs.c.status.in_(ACTIVE_RUN_STATUSES),
                    )
                ).scalar_one()
            )

    @_instrumented
    def claim_next_run(self, *, worker_id: str) -> dict[str, Any] | None:
        """Atomically claim the oldest queued run that carries a request payload.

        The conditional UPDATE (`... AND status = 'queued'`) guarantees that two
        workers racing for the same row cannot both claim it.
        """
        with self.begin() as connection:
            candidates = connection.execute(
                sa.select(workflow_runs.c.id)
                .where(
                    workflow_runs.c.status == "queued",
                    workflow_runs.c.request_json.is_not(None),
                    workflow_runs.c.cancel_requested == 0,
                )
                .order_by(workflow_runs.c.started_at)
                .limit(5)
            ).all()
            for (run_id,) in candidates:
                result = connection.execute(
                    workflow_runs.update()
                    .where(workflow_runs.c.id == run_id, workflow_runs.c.status == "queued")
                    .values(status="running", worker_id=worker_id)
                )
                if result.rowcount == 1:
                    row = (
                        connection.execute(sa.select(workflow_runs).where(workflow_runs.c.id == run_id))
                        .mappings()
                        .first()
                    )
                    item = self._run_row(row)
                    item["request"] = _loads(row["request_json"], None)
                    return item
        return None

    # ---------------------------------------------------------------- events

    @_instrumented
    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        node_id: str | None = None,
        parent_node_id: str | None = None,
        node_type: str | None = None,
        label: str | None = None,
        status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.append_events(
            run_id,
            [
                {
                    "type": event_type,
                    "node_id": node_id,
                    "parent_node_id": parent_node_id,
                    "node_type": node_type,
                    "label": label,
                    "status": status,
                    "details": details or {},
                }
            ],
        )[0]

    @_instrumented
    def append_events(self, run_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        with self.begin() as connection:
            for event in events:
                event_id = f"evt_{uuid4().hex[:16]}"
                created_at = utc_now()
                payload = event.get("details") or {}
                result = connection.execute(
                    execution_events.insert().values(
                        id=event_id,
                        run_id=run_id,
                        event_type=event["type"],
                        node_id=event.get("node_id"),
                        parent_node_id=event.get("parent_node_id"),
                        node_type=event.get("node_type"),
                        label=event.get("label"),
                        status=event.get("status"),
                        details_json=_json(payload),
                        created_at=created_at,
                    )
                )
                stored.append(
                    {
                        "sequence": result.inserted_primary_key[0],
                        "id": event_id,
                        "type": event["type"],
                        "run_id": run_id,
                        "node_id": event.get("node_id"),
                        "parent_node_id": event.get("parent_node_id"),
                        "node_type": event.get("node_type"),
                        "label": event.get("label"),
                        "status": event.get("status"),
                        "details": payload,
                        "timestamp": created_at,
                    }
                )
        return stored

    @_instrumented
    def list_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.begin() as connection:
            rows = (
                connection.execute(
                    sa.select(execution_events)
                    .where(execution_events.c.run_id == run_id, execution_events.c.sequence > after)
                    .order_by(execution_events.c.sequence)
                )
                .mappings()
                .all()
            )
        return [self._event_row(row) for row in rows]

    # ----------------------------------------------------------------- steps

    STEP_COLUMNS = {
        "agent",
        "status",
        "attempts",
        "depends_on_json",
        "error_json",
        "output_preview",
        "latency_ms",
        "started_at",
        "completed_at",
    }

    @_instrumented
    def upsert_step(self, run_id: str, step_id: str, **values: Any) -> None:
        updates = {key: value for key, value in values.items() if key in self.STEP_COLUMNS}
        updates.setdefault("status", "PENDING")
        with self.begin() as connection:
            self._upsert(
                connection,
                run_steps,
                {"run_id": run_id, "step_id": step_id, **updates},
                conflict_columns=["run_id", "step_id"],
                update_columns=[key for key in updates],
            )

    @_instrumented
    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self.begin() as connection:
            rows = (
                connection.execute(
                    sa.select(run_steps)
                    .where(run_steps.c.run_id == run_id)
                    .order_by(run_steps.c.started_at.is_(None), run_steps.c.started_at)
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            item = dict(row)
            item["depends_on"] = _loads(item.pop("depends_on_json", None), [])
            item["error"] = _loads(item.pop("error_json", None), None)
            items.append(item)
        return items

    # ----------------------------------------------------------------- spans

    @_instrumented
    def save_spans(self, run_id: str, spans: list[dict[str, Any]]) -> None:
        if not spans:
            return
        with self.begin() as connection:
            connection.execute(
                run_spans.insert(),
                [
                    {
                        "run_id": run_id,
                        "span_id": span["span_id"],
                        "parent_id": span.get("parent_id"),
                        "kind": span["kind"],
                        "name": span["name"][:160],
                        "status": span["status"],
                        "latency_ms": float(span["latency_ms"]),
                        "attributes_json": _json(span.get("attributes") or {}),
                        "started_at": span["started_at"],
                    }
                    for span in spans
                ],
            )

    @_instrumented
    def list_spans(self, run_id: str) -> list[dict[str, Any]]:
        with self.begin() as connection:
            rows = (
                connection.execute(
                    sa.select(run_spans)
                    .where(run_spans.c.run_id == run_id)
                    .order_by(run_spans.c.started_at)
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            item = dict(row)
            item["attributes"] = _loads(item.pop("attributes_json", None), {})
            items.append(item)
        return items

    @_instrumented
    def latency_summary(self, *, user_id: str, run_limit: int = 200) -> dict[str, Any]:
        """Aggregate recent spans into per-(kind, name) latency statistics."""
        with self.begin() as connection:
            recent = (
                sa.select(workflow_runs.c.id)
                .where(workflow_runs.c.user_id == user_id)
                .order_by(workflow_runs.c.started_at.desc())
                .limit(run_limit)
                .subquery()
            )
            rows = connection.execute(
                sa.select(
                    run_spans.c.kind,
                    run_spans.c.name,
                    run_spans.c.latency_ms,
                    run_spans.c.status,
                    run_spans.c.attributes_json,
                ).where(run_spans.c.run_id.in_(sa.select(recent.c.id)))
            ).all()
        groups: dict[tuple[str, str], list[tuple[float, str, dict[str, Any]]]] = {}
        for kind, name, latency, status, attributes_json in rows:
            groups.setdefault((kind, name), []).append((latency, status, _loads(attributes_json, {})))
        items = []
        for (kind, name), values in groups.items():
            latencies = sorted(value[0] for value in values)
            count = len(latencies)
            items.append(
                {
                    "kind": kind,
                    "name": name,
                    "count": count,
                    "total_ms": round(sum(latencies), 1),
                    "avg_ms": round(sum(latencies) / count, 1),
                    "p50_ms": round(latencies[min(count - 1, int(0.50 * count))], 1),
                    "p95_ms": round(latencies[min(count - 1, int(0.95 * count))], 1),
                    "error_rate": round(sum(1 for value in values if value[1] != "success") / count, 3),
                    "tokens": sum(int(value[2].get("total_tokens", 0) or 0) for value in values),
                }
            )
        items.sort(key=lambda item: item["total_ms"], reverse=True)
        return {"spans_analyzed": len(rows), "contributors": items}

    # ------------------------------------------------------------- documents

    @_instrumented
    def create_document(
        self,
        *,
        user_id: str,
        project_id: str,
        name: str,
        content_type: str | None,
        storage_path: str,
        size: int,
    ) -> dict[str, Any]:
        document_id = f"doc_{uuid4().hex[:16]}"
        now = utc_now()
        with self.begin() as connection:
            connection.execute(
                documents.insert().values(
                    id=document_id,
                    user_id=user_id,
                    project_id=project_id,
                    name=name,
                    content_type=content_type,
                    storage_path=storage_path,
                    size=size,
                    status="uploaded",
                    chunk_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        return self.get_document(document_id, user_id=user_id) or {}

    @_instrumented
    def update_document(self, document_id: str, **values: Any) -> None:
        allowed = {"status", "chunk_count", "embedding_model", "error", "storage_path"}
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["updated_at"] = utc_now()
        with self.begin() as connection:
            connection.execute(
                documents.update().where(documents.c.id == document_id).values(**updates)
            )

    @_instrumented
    def replace_chunks(self, document_id: str, chunks: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self.begin() as connection:
            connection.execute(
                document_chunks.delete().where(document_chunks.c.document_id == document_id)
            )
            if chunks:
                connection.execute(
                    document_chunks.insert(),
                    [
                        {
                            "id": chunk["id"],
                            "document_id": document_id,
                            "chunk_index": chunk["chunk_index"],
                            "page_number": chunk.get("page_number"),
                            "content": chunk["content"],
                            "embedding_json": "",
                            "embedding_blob": _vector_blob(chunk["embedding"]),
                            "created_at": now,
                        }
                        for chunk in chunks
                    ],
                )
                if self.vector_enabled:
                    dims = get_settings().embedding_dimensions
                    updates = [
                        {"id": chunk["id"], "v": _vector_literal(chunk["embedding"])}
                        for chunk in chunks
                        if len(chunk["embedding"]) == dims
                    ]
                    if updates:
                        connection.execute(self._vector_update_statement(), updates)

    @_instrumented
    def list_documents(
        self, *, user_id: str = "local-user", project_id: str | None = None
    ) -> list[dict[str, Any]]:
        statement = sa.select(documents).where(documents.c.user_id == user_id)
        if project_id:
            statement = statement.where(documents.c.project_id == project_id)
        statement = statement.order_by(documents.c.created_at.desc())
        with self.begin() as connection:
            return [dict(row) for row in connection.execute(statement).mappings().all()]

    @_instrumented
    def get_document(self, document_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(documents).where(
                        documents.c.id == document_id, documents.c.user_id == user_id
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    @_instrumented
    def delete_document(self, document_id: str, *, user_id: str) -> dict[str, Any] | None:
        document = self.get_document(document_id, user_id=user_id)
        if not document:
            return None
        with self.begin() as connection:
            connection.execute(
                document_chunks.delete().where(document_chunks.c.document_id == document_id)
            )
            connection.execute(
                documents.delete().where(
                    documents.c.id == document_id, documents.c.user_id == user_id
                )
            )
        return document

    @_instrumented
    def search_chunks(
        self,
        query_embedding: list[float],
        *,
        user_id: str,
        project_id: str | None,
        top_k: int,
        threshold: float,
        document_ids: list[str] | None = None,
        embedding_model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Vector search scoped to the caller's documents.

        PostgreSQL + pgvector: ordered by cosine distance in the database (HNSW
        index). Otherwise: only ids and vectors are loaded and scored with
        NumPy. In both cases chunk text is fetched for the top-k rows only.
        """
        filters = dict(
            user_id=user_id,
            project_id=project_id,
            top_k=top_k,
            threshold=threshold,
            document_ids=document_ids,
            embedding_model=embedding_model,
        )
        with self.begin() as connection:
            if self.vector_enabled and len(query_embedding) == get_settings().embedding_dimensions:
                winners = self._pgvector_winners(connection, query_embedding, **filters)
            else:
                winners = self._numpy_winners(connection, query_embedding, **filters)
            if not winners:
                return []
            rows = (
                connection.execute(
                    sa.select(
                        document_chunks.c.id,
                        document_chunks.c.document_id,
                        document_chunks.c.chunk_index,
                        document_chunks.c.page_number,
                        document_chunks.c.content,
                        document_chunks.c.created_at,
                        documents.c.name.label("document_name"),
                        documents.c.user_id,
                        documents.c.project_id,
                    )
                    .join(documents, documents.c.id == document_chunks.c.document_id)
                    .where(document_chunks.c.id.in_([chunk_id for chunk_id, _ in winners]))
                )
                .mappings()
                .all()
            )
        by_id = {row["id"]: dict(row) for row in rows}
        results = []
        for chunk_id, score in winners:
            item = by_id.get(chunk_id)
            if item is None:
                continue
            item["similarity"] = round(score, 6)
            results.append(item)
        return results

    @staticmethod
    def _numpy_winners(connection, query_embedding, *, user_id, project_id, top_k, threshold, document_ids, embedding_model):
        scope = (
            sa.select(document_chunks.c.id, document_chunks.c.embedding_blob, document_chunks.c.embedding_json)
            .join(documents, documents.c.id == document_chunks.c.document_id)
            .where(documents.c.user_id == user_id, documents.c.status == "indexed")
        )
        if project_id:
            scope = scope.where(documents.c.project_id == project_id)
        if document_ids:
            scope = scope.where(documents.c.id.in_(document_ids))
        if embedding_model:
            scope = scope.where(documents.c.embedding_model == embedding_model)
        ids: list[str] = []
        vectors: list[np.ndarray] = []
        query = np.asarray(query_embedding, dtype=np.float32)
        for chunk_id, blob, legacy in connection.execute(scope).all():
            vector = _vector_from_row(blob, legacy)
            if vector is None or vector.shape != query.shape:
                continue
            ids.append(chunk_id)
            vectors.append(vector)
        if not ids:
            return []
        scores = _cosine_scores(np.vstack(vectors), query)
        order = np.argsort(-scores)
        return [(ids[i], float(scores[i])) for i in order[: max(1, top_k)] if scores[i] >= threshold]

    def _pgvector_winners(self, connection, query_embedding, *, user_id, project_id, top_k, threshold, document_ids, embedding_model):
        conditions = ["d.user_id = :user_id", "d.status = 'indexed'", "c.embedding_vec IS NOT NULL"]
        params: dict[str, Any] = {"q": _vector_literal(query_embedding), "user_id": user_id, "k": max(1, top_k)}
        if project_id:
            conditions.append("d.project_id = :project_id")
            params["project_id"] = project_id
        if document_ids:
            conditions.append("d.id = ANY(:document_ids)")
            params["document_ids"] = list(document_ids)
        if embedding_model:
            conditions.append("d.embedding_model = :embedding_model")
            params["embedding_model"] = embedding_model
        # Keep filtered HNSW results exact (pgvector >= 0.8); ignored otherwise.
        try:
            with connection.begin_nested():
                connection.execute(sa.text("SET LOCAL hnsw.iterative_scan = strict_order"))
        except Exception:
            pass
        where = " AND ".join(conditions)
        distance = f'c.embedding_vec OPERATOR("{self._vector_schema}".<=>) CAST(:q AS {self._vector_type})'
        rows = connection.execute(
            sa.text(
                f"SELECT c.id, 1 - ({distance}) AS similarity "
                "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
                f"WHERE {where} ORDER BY {distance} LIMIT :k"
            ),
            params,
        ).all()
        return [(chunk_id, float(score)) for chunk_id, score in rows if float(score) >= threshold]

    # -------------------------------------------------------------- memories

    @_instrumented
    def save_memory(
        self,
        *,
        user_id: str,
        project_id: str | None,
        memory_type: str,
        content: str,
        importance_score: float,
        embedding: list[float],
        source_session_id: str | None = None,
        source_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        memory_id = f"mem_{uuid4().hex[:16]}"
        with self.begin() as connection:
            existing_query = sa.select(memories.c.id).where(
                memories.c.user_id == user_id, memories.c.content == content
            )
            existing_query = existing_query.where(
                memories.c.project_id.is_(None) if project_id is None else memories.c.project_id == project_id
            )
            existing = connection.execute(existing_query).first()
            if existing:
                memory_id = existing[0]
            else:
                connection.execute(
                    memories.insert().values(
                        id=memory_id,
                        user_id=user_id,
                        project_id=project_id,
                        memory_type=memory_type,
                        content=content,
                        importance_score=importance_score,
                        source_session_id=source_session_id,
                        source_run_id=source_run_id,
                        embedding_json="",
                        embedding_blob=_vector_blob(embedding) if embedding else None,
                        metadata_json=_json(metadata or {}),
                        created_at=utc_now(),
                    )
                )
        return self.get_memory(memory_id, user_id=user_id) or {}

    @_instrumented
    def get_memory(self, memory_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(memories).where(memories.c.id == memory_id, memories.c.user_id == user_id)
                )
                .mappings()
                .first()
            )
        return self._memory_row(row) if row else None

    @_instrumented
    def list_memories(
        self, *, user_id: str, project_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        statement = sa.select(memories).where(memories.c.user_id == user_id)
        if project_id:
            statement = statement.where(
                sa.or_(memories.c.project_id == project_id, memories.c.project_id.is_(None))
            )
        statement = statement.order_by(
            memories.c.importance_score.desc(), memories.c.created_at.desc()
        ).limit(limit)
        with self.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._memory_row(row) for row in rows]

    @_instrumented
    def search_memories(
        self,
        query_embedding: list[float],
        *,
        user_id: str,
        project_id: str | None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        items = self.list_memories(user_id=user_id, project_id=project_id, limit=200)
        if not items:
            return []
        query = np.asarray(query_embedding, dtype=np.float32)
        for item in items:
            vector = item.pop("embedding", None)
            if vector is None or len(vector) != len(query):
                item["similarity"] = 0.0
            else:
                item["similarity"] = float(
                    _cosine_scores(np.asarray([vector], dtype=np.float32), query)[0]
                )
        items.sort(
            key=lambda item: (item["similarity"] * 0.8) + (item["importance_score"] * 0.2),
            reverse=True,
        )
        return items[:limit]

    @_instrumented
    def delete_memory(self, memory_id: str, *, user_id: str) -> bool:
        with self.begin() as connection:
            result = connection.execute(
                memories.delete().where(memories.c.id == memory_id, memories.c.user_id == user_id)
            )
        return result.rowcount > 0

    # ------------------------------------------------------ reports & evals

    @_instrumented
    def save_report(
        self,
        *,
        run_id: str,
        user_id: str,
        project_id: str,
        title: str,
        content: str,
        output_format: str = "markdown",
    ) -> dict[str, Any]:
        with self.begin() as connection:
            self._upsert(
                connection,
                reports,
                {
                    "id": f"report_{uuid4().hex[:16]}",
                    "run_id": run_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "title": title[:255],
                    "format": output_format,
                    "content": content,
                    "created_at": utc_now(),
                },
                conflict_columns=["run_id"],
                update_columns=["title", "format", "content", "created_at"],
            )
        return {"run_id": run_id, "title": title[:255], "format": output_format}

    @_instrumented
    def list_reports(self, *, user_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
        statement = (
            sa.select(reports, workflow_runs.c.status, workflow_runs.c.active_agent)
            .join(workflow_runs, workflow_runs.c.id == reports.c.run_id)
            .where(reports.c.user_id == user_id)
        )
        if project_id:
            statement = statement.where(reports.c.project_id == project_id)
        statement = statement.order_by(reports.c.created_at.desc())
        with self.begin() as connection:
            return [dict(row) for row in connection.execute(statement).mappings().all()]

    @_instrumented
    def get_report(self, report_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(reports).where(reports.c.id == report_id, reports.c.user_id == user_id)
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    @_instrumented
    def get_report_by_run(self, run_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(reports).where(reports.c.run_id == run_id, reports.c.user_id == user_id)
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    @_instrumented
    def save_evaluation(
        self,
        *,
        run_id: str,
        user_id: str,
        project_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        with self.begin() as connection:
            self._upsert(
                connection,
                evaluations,
                {
                    "id": f"eval_{uuid4().hex[:16]}",
                    "run_id": run_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "result_json": _json(result),
                    "created_at": utc_now(),
                },
                conflict_columns=["run_id"],
                update_columns=["result_json", "created_at"],
            )
        return {"run_id": run_id, **result}

    @_instrumented
    def get_evaluation(self, run_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(evaluations).where(
                        evaluations.c.run_id == run_id, evaluations.c.user_id == user_id
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            return None
        return self._evaluation_row(row)

    @_instrumented
    def get_evaluation_by_id(self, evaluation_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(evaluations).where(
                        evaluations.c.id == evaluation_id, evaluations.c.user_id == user_id
                    )
                )
                .mappings()
                .first()
            )
        return self._evaluation_row(row) if row else None

    @_instrumented
    def list_evaluations(
        self, *, user_id: str, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        statement = sa.select(evaluations).where(evaluations.c.user_id == user_id)
        if project_id:
            statement = statement.where(evaluations.c.project_id == project_id)
        statement = statement.order_by(evaluations.c.created_at.desc())
        with self.begin() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._evaluation_row(row) for row in rows]

    # ------------------------------------------------------------- workflows

    @_instrumented
    def save_workflow(
        self,
        *,
        user_id: str,
        project_id: str,
        name: str,
        definition: dict[str, Any],
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        resolved_id = workflow_id or f"workflow_{uuid4().hex[:16]}"
        with self.begin() as connection:
            self._upsert(
                connection,
                workflows,
                {
                    "id": resolved_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "name": name,
                    "definition_json": _json(definition),
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["id"],
                update_columns=["name", "definition_json", "project_id", "updated_at"],
            )
        return self.get_workflow(resolved_id, user_id=user_id) or {}

    @_instrumented
    def list_workflows(self, *, user_id: str) -> list[dict[str, Any]]:
        with self.begin() as connection:
            rows = (
                connection.execute(
                    sa.select(workflows)
                    .where(workflows.c.user_id == user_id)
                    .order_by(workflows.c.updated_at.desc())
                )
                .mappings()
                .all()
            )
        return [self._workflow_row(row) for row in rows]

    @_instrumented
    def get_workflow(self, workflow_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(workflows).where(
                        workflows.c.id == workflow_id, workflows.c.user_id == user_id
                    )
                )
                .mappings()
                .first()
            )
        return self._workflow_row(row) if row else None

    @_instrumented
    def delete_workflow(self, workflow_id: str, *, user_id: str) -> bool:
        with self.begin() as connection:
            result = connection.execute(
                workflows.delete().where(workflows.c.id == workflow_id, workflows.c.user_id == user_id)
            )
        return result.rowcount > 0

    # ------------------------------------------------------------------ chat

    @_instrumented
    def create_chat_session(
        self, *, user_id: str, title: str = "New chat", session_id: str | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        chat_id = session_id or f"chat_{uuid4().hex[:12]}"
        with self.begin() as connection:
            self._upsert(
                connection,
                chat_sessions,
                {
                    "id": chat_id,
                    "user_id": user_id,
                    "title": title.strip()[:80] or "New chat",
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["id"],
                update_columns=None,
            )
        # A session id owned by another user is never returned.
        return self.get_chat_session(chat_id, user_id=user_id) or {}

    @_instrumented
    def get_chat_session(self, session_id: str, *, user_id: str) -> dict[str, Any] | None:
        with self.begin() as connection:
            row = (
                connection.execute(
                    sa.select(
                        chat_sessions.c.id,
                        chat_sessions.c.title,
                        chat_sessions.c.created_at,
                        chat_sessions.c.updated_at,
                    ).where(chat_sessions.c.id == session_id, chat_sessions.c.user_id == user_id)
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    @_instrumented
    def chat_session_exists(self, session_id: str) -> bool:
        with self.begin() as connection:
            return (
                connection.execute(
                    sa.select(chat_sessions.c.id).where(chat_sessions.c.id == session_id)
                ).first()
                is not None
            )

    @_instrumented
    def list_chat_sessions(self, *, user_id: str) -> list[dict[str, Any]]:
        first = chat_messages.alias("first_message")
        first_message = (
            sa.select(first.c.content)
            .where(first.c.session_id == chat_sessions.c.id, first.c.role == "user")
            .order_by(first.c.created_at)
            .limit(1)
            .correlate(chat_sessions)
            .scalar_subquery()
        )
        message_count = sa.func.count(chat_messages.c.id)
        statement = (
            sa.select(
                chat_sessions.c.id,
                sa.case(
                    (chat_sessions.c.title == "New chat", sa.func.coalesce(first_message, chat_sessions.c.title)),
                    else_=chat_sessions.c.title,
                ).label("title"),
                chat_sessions.c.created_at,
                chat_sessions.c.updated_at,
                message_count.label("message_count"),
            )
            .select_from(chat_sessions.outerjoin(chat_messages, chat_messages.c.session_id == chat_sessions.c.id))
            .where(chat_sessions.c.user_id == user_id)
            .group_by(chat_sessions.c.id, chat_sessions.c.title, chat_sessions.c.created_at, chat_sessions.c.updated_at)
            .having(message_count > 0)
            .order_by(chat_sessions.c.updated_at.desc())
        )
        with self.begin() as connection:
            return [dict(row) for row in connection.execute(statement).mappings().all()]

    @_instrumented
    def add_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        attachment_name: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        message = {
            "id": f"msg_{uuid4().hex[:12]}",
            "session_id": session_id,
            "role": role,
            "content": content,
            "attachment_name": attachment_name,
            "created_at": now,
        }
        with self.begin() as connection:
            connection.execute(chat_messages.insert().values(**message))
            connection.execute(
                chat_sessions.update().where(chat_sessions.c.id == session_id).values(updated_at=now)
            )
        return message

    @_instrumented
    def list_chat_messages(
        self, session_id: str, *, user_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        statement = (
            sa.select(
                chat_messages.c.id,
                chat_messages.c.session_id,
                chat_messages.c.role,
                chat_messages.c.content,
                chat_messages.c.attachment_name,
                chat_messages.c.created_at,
            )
            .join(chat_sessions, chat_sessions.c.id == chat_messages.c.session_id)
            .where(chat_messages.c.session_id == session_id, chat_sessions.c.user_id == user_id)
        )
        if limit:
            statement = statement.order_by(chat_messages.c.created_at.desc()).limit(limit)
            with self.begin() as connection:
                rows = [dict(row) for row in connection.execute(statement).mappings().all()]
            return list(reversed(rows))
        statement = statement.order_by(chat_messages.c.created_at)
        with self.begin() as connection:
            return [dict(row) for row in connection.execute(statement).mappings().all()]

    @_instrumented
    def delete_chat_session(self, session_id: str, *, user_id: str) -> bool:
        with self.begin() as connection:
            owned = connection.execute(
                sa.select(chat_sessions.c.id).where(
                    chat_sessions.c.id == session_id, chat_sessions.c.user_id == user_id
                )
            ).first()
            if not owned:
                return False
            connection.execute(chat_messages.delete().where(chat_messages.c.session_id == session_id))
            connection.execute(chat_sessions.delete().where(chat_sessions.c.id == session_id))
        return True

    # --------------------------------------------------------------- mappers

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["route"] = _loads(item.pop("route_json", None), None)
        item["plan"] = _loads(item.pop("plan_json", None), None)
        item["artifacts"] = _loads(item.pop("artifacts_json", None), [])
        item["failure"] = _loads(item.pop("error_json", None), None)
        item["metrics"] = _loads(item.pop("metrics_json", None), None)
        item["guardrails"] = _loads(item.pop("guardrails_json", None), None)
        item.pop("request_json", None)
        item["needs_clarification"] = bool(item.get("needs_clarification"))
        item["cancel_requested"] = bool(item.get("cancel_requested"))
        item["document_ids"] = []
        return item

    @staticmethod
    def _event_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["type"] = item.pop("event_type")
        item["details"] = _loads(item.pop("details_json", None), {})
        item["timestamp"] = item.pop("created_at")
        return item

    @staticmethod
    def _memory_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        vector = _vector_from_row(item.pop("embedding_blob", None), item.pop("embedding_json", None))
        item["embedding"] = vector.tolist() if vector is not None else []
        item["metadata"] = _loads(item.pop("metadata_json", None), {})
        return item

    @staticmethod
    def _workflow_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["definition"] = _loads(item.pop("definition_json", None), {})
        return item

    @staticmethod
    def _evaluation_row(row: Any) -> dict[str, Any]:
        result = _loads(row["result_json"], {})
        result.update({"id": row["id"], "run_id": row["run_id"], "created_at": row["created_at"]})
        return result


def _vector_literal(vector: Any) -> str:
    return "[" + ",".join(f"{float(value):.7g}" for value in vector) + "]"


def _cosine_scores(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    row_norms = np.linalg.norm(matrix, axis=1)
    row_norms[row_norms == 0] = 1.0
    return (matrix @ query) / (row_norms * query_norm)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(
        _cosine_scores(np.asarray([left], dtype=np.float32), np.asarray(right, dtype=np.float32))[0]
    )


runtime_store = RuntimeStore()
