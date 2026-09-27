"""Read-only SQL tools over an operator-configured database.

`SQL_AGENT_DATABASE_URL` (any SQLAlchemy URL) enables them. Layers:

1. Parsing (sqlglot): exactly one statement, and it must be a query
   (SELECT / UNION / ...). Any write, DDL, `SELECT ... INTO`, data-modifying
   CTE, PRAGMA/command or dangerous function is rejected.
2. Table allowlist: every referenced table must be in
   `SQL_AGENT_ALLOWED_TABLES` (or, when that is empty, among the database's
   own user tables, which excludes system catalogs such as sqlite_master,
   pg_catalog and information_schema).
3. The query is re-generated from the AST and wrapped with a row limit.
4. Database-level read-only execution: `PRAGMA query_only` (SQLite),
   `SET TRANSACTION READ ONLY` + `statement_timeout` (PostgreSQL),
   `SET SESSION TRANSACTION READ ONLY` (MySQL). Every transaction is rolled
   back. SQLite queries are aborted at the time limit.

Use a database role that only has SELECT on the allowed tables; these checks
are defense in depth, not a replacement for database permissions.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.errors import InvalidInputError, StepTimeoutError, ToolError
from app.mcp.schemas import ToolContext

DANGEROUS_FUNCTIONS = {
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export",
    "dblink", "dblink_exec", "pg_terminate_backend", "pg_cancel_backend", "set_config", "pg_reload_conf",
    "sleep", "benchmark", "load_file",
}
DIALECTS = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "mssql": "tsql"}
MAX_COLUMNS_DESCRIBED = 60
MAX_TABLES_DESCRIBED = 60


class SqlQueryInput(BaseModel):
    sql: str = Field(min_length=1, max_length=10_000)


class SqlSchemaInput(BaseModel):
    tables: list[str] = Field(default_factory=list, max_length=MAX_TABLES_DESCRIBED)


def available() -> str:
    return "available" if get_settings().sql_agent_database_url else "not_configured"


@lru_cache(maxsize=4)
def _engine(url: str) -> sa.Engine:
    engine = sa.create_engine(url, pool_pre_ping=True, future=True)
    if engine.dialect.name == "sqlite":

        @sa.event.listens_for(engine, "connect")
        def _read_only(dbapi_connection, _record):  # pragma: no cover - driver hook
            dbapi_connection.execute("PRAGMA query_only = ON")

    return engine


def engine() -> sa.Engine:
    url = get_settings().sql_agent_database_url
    if not url:
        raise ToolError("No SQL database is configured (SQL_AGENT_DATABASE_URL).", retryable=False)
    return _engine(url)


def dialect_name() -> str:
    return DIALECTS.get(engine().dialect.name, engine().dialect.name)


def allowed_tables() -> dict[str, str]:
    """lower-case name -> real name of every table the agent may query."""
    existing = {name.lower(): name for name in sa.inspect(engine()).get_table_names()}
    configured = [name.strip().lower() for name in (get_settings().sql_agent_allowed_tables or "").split(",") if name.strip()]
    if configured:
        return {name: existing[name] for name in configured if name in existing}
    return existing


def validate_sql(sql: str, *, tables: dict[str, str], dialect: str, max_rows: int) -> str:
    """Return a safe, row-limited query or raise InvalidInputError."""
    import sqlglot
    from sqlglot import exp

    try:
        statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement is not None]
    except sqlglot.errors.ParseError as exc:
        raise InvalidInputError(f"SQL could not be parsed: {str(exc).splitlines()[0][:200]}") from None
    if len(statements) != 1:
        raise InvalidInputError("Exactly one SQL statement is allowed.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise InvalidInputError(f"Only read-only queries are allowed (got {tree.key.upper()}).")
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Into, exp.Pragma, exp.Set, exp.TruncateTable)
    for node in tree.walk():
        if isinstance(node, forbidden):
            raise InvalidInputError(f"Statement contains a forbidden operation ({node.key.upper()}).")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in DANGEROUS_FUNCTIONS:
                raise InvalidInputError(f"Function {name} is not allowed.")
    if tree.args.get("into"):
        raise InvalidInputError("SELECT ... INTO is not allowed.")
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    referenced = {table.name.lower() for table in tree.find_all(exp.Table) if table.name}
    unknown = sorted(referenced - cte_names - set(tables))
    if unknown:
        raise InvalidInputError(f"Tables not allowed or not found: {unknown}. Allowed: {sorted(tables.values())}.")
    if not referenced - cte_names:
        raise InvalidInputError("The query must read from at least one allowed table.")
    return f"SELECT * FROM ({tree.sql(dialect=dialect)}) AS sql_agent_result LIMIT {int(max_rows) + 1}"


def _execute(query: str, timeout_seconds: float) -> tuple[list[str], list[list[Any]]]:
    eng = engine()
    with eng.connect() as connection:
        deadline = time.monotonic() + timeout_seconds
        raw = connection.connection.driver_connection
        name = eng.dialect.name
        try:
            if name == "sqlite":
                raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            elif name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}")
            elif name in {"mysql", "mariadb"}:
                connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                connection.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_seconds * 1000)}")
            result = connection.exec_driver_sql(query)
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchall()]
        except sa.exc.OperationalError as exc:
            if time.monotonic() > deadline or "interrupted" in str(exc).lower() or "timeout" in str(exc).lower():
                raise StepTimeoutError(f"SQL query exceeded {timeout_seconds:.0f}s.") from None
            raise InvalidInputError(f"SQL error: {str(exc.orig)[:300]}") from None
        except sa.exc.DBAPIError as exc:
            raise InvalidInputError(f"SQL error: {str(exc.orig)[:300]}") from None
        finally:
            if name == "sqlite":
                raw.set_progress_handler(None, 0)
            connection.rollback()
    return columns, rows


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)



def sql_query(arguments: SqlQueryInput, context: ToolContext) -> dict[str, Any]:
    settings = get_settings()
    tables = allowed_tables()
    if not tables:
        raise ToolError("No allowed tables exist in the configured database.", retryable=False)
    query = validate_sql(arguments.sql, tables=tables, dialect=dialect_name(), max_rows=settings.sql_agent_max_rows)
    columns, rows = _execute(query, settings.sql_agent_timeout_seconds)
    truncated = len(rows) > settings.sql_agent_max_rows
    rows = rows[: settings.sql_agent_max_rows]
    return {
        "sql": query,
        "columns": columns,
        "rows": [[_jsonable(value) for value in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


def sql_schema(arguments: SqlSchemaInput, context: ToolContext) -> dict[str, Any]:
    tables = allowed_tables()
    wanted = {name.lower() for name in arguments.tables} or set(tables)
    inspector = sa.inspect(engine())
    described = {}
    for key in sorted(wanted & set(tables))[:MAX_TABLES_DESCRIBED]:
        name = tables[key]
        described[name] = [
            {"name": column["name"], "type": str(column["type"])}
            for column in inspector.get_columns(name)[:MAX_COLUMNS_DESCRIBED]
        ]
    return {"dialect": dialect_name(), "tables": described}
