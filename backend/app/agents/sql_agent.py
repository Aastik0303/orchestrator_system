"""Read-only SQL agent.

Uses the operator-configured database (`SQL_AGENT_DATABASE_URL`) through the
`data.sql_schema` / `data.sql_query` tools, which validate every statement
(single read-only query, allowlisted tables, row limit) and execute it in a
read-only transaction. The SQL is either supplied by the user in a ```sql
block or written by the model from the allowed schema; a generated query that
fails validation or execution gets one repair attempt.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.context import AgentContext
from app.agents.prompts import system_prompt, wrap_untrusted
from app.agents.registry import ModelPolicy, PermissionPolicy, RetryPolicy, register_agent
from app.core.errors import InvalidInputError
from app.models import AgentResult, AgentTask

SQL_BLOCK_RE = re.compile(r"```sql\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
WRITER_ROLE = (
    "You write exactly one read-only SQL query (SELECT, optionally WITH) for the {dialect} "
    "dialect that answers the user's request. Use only the tables and columns in the schema. "
    "Never write INSERT/UPDATE/DELETE/DDL. Reply with a single ```sql code block and nothing else."
)
MAX_ROWS_SHOWN = 20


def extract_sql(text: str) -> str | None:
    match = SQL_BLOCK_RE.search(text)
    return match.group(1).strip() if match else None


def _schema_text(schema: dict[str, Any]) -> str:
    lines = []
    for table, columns in schema["tables"].items():
        rendered = ", ".join(f"{column['name']} {column['type']}" for column in columns)
        lines.append(f"{table}({rendered})")
    return "\n".join(lines)


def _markdown_table(columns: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        text = "NULL" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", " ")[:80]

    header = "| " + " | ".join(cell(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows[:MAX_ROWS_SHOWN]]
    return "\n".join([header, divider, *body])


async def _write_sql(task: AgentTask, ctx: AgentContext, schema: dict[str, Any], error: str | None = None, previous: str | None = None) -> str | None:
    user = f"Request:\n{task.instruction or task.goal}\n\n" + wrap_untrusted("schema", _schema_text(schema))
    if error and previous:
        user += f"\n\nYour previous query failed:\n```sql\n{previous}\n```\nError: {error}\nWrite a corrected query."
    response = await ctx.llm(
        system=system_prompt(WRITER_ROLE.format(dialect=schema["dialect"]), output_rules=False),
        user=user,
        name="sql_agent.write",
    )
    return extract_sql(response.text) if response else None


@register_agent(
    name="sql_agent",
    description="Answers questions with validated, read-only SQL over the configured database (allowlisted tables, row limits).",
    capabilities=["sql_query"],
    tools=["data.sql_schema", "data.sql_query"],
    timeout_seconds=60,
    retry_policy=RetryPolicy(max_retries=0),
    model_policy=ModelPolicy(tier="fast", temperature=0.0, max_output_tokens=900),
    token_budget=4000,
    permission_policy=PermissionPolicy(granted_permissions={"db:read"}),
)
async def sql_agent(task: AgentTask, ctx: AgentContext) -> AgentResult:
    if not ctx.run.tools.is_available("data.sql_query"):
        return AgentResult(
            summary="The SQL request was routed correctly, but no read-only database connection is configured.",
            warnings=["Set SQL_AGENT_DATABASE_URL (a SELECT-only role) and optionally SQL_AGENT_ALLOWED_TABLES."],
            metadata={"tool_success": False},
        )
    schema = await ctx.call_tool("data.sql_schema", {})
    if not schema["tables"]:
        return AgentResult(
            summary="The configured database exposes no allowed tables.",
            warnings=["Check SQL_AGENT_ALLOWED_TABLES against the database's table names."],
            metadata={"tool_success": False},
        )
    sql = extract_sql(task.goal)
    generated = sql is None
    if generated:
        sql = await _write_sql(task, ctx, schema)
    if not sql:
        return AgentResult(
            summary="No SQL query could be obtained for this request.",
            warnings=["Include the query in a ```sql block, or configure a model provider that can write it."],
            findings=[f"Queryable tables: {', '.join(schema['tables'])}."],
            metadata={"tool_success": False},
        )
    try:
        result = await ctx.call_tool("data.sql_query", {"sql": sql})
    except InvalidInputError as exc:
        if not generated:
            return AgentResult(
                summary=f"The query was rejected: {str(exc)[:300]}",
                artifacts=[{"type": "sql", "sql": sql}],
                warnings=["Only single read-only queries over the allowed tables can run."],
                metadata={"tool_success": False},
            )
        repaired = await _write_sql(task, ctx, schema, error=str(exc)[:300], previous=sql)
        if not repaired:
            raise
        sql = repaired
        result = await ctx.call_tool("data.sql_query", {"sql": sql})

    rows = result["rows"]
    summary = [f"The query returned {result['row_count']} row{'s' if result['row_count'] != 1 else ''}"
               + (" (truncated at the row limit)." if result["truncated"] else ".")]
    summary.append("```sql\n" + sql + "\n```")
    if rows:
        summary.append(_markdown_table(result["columns"], rows))
        if len(rows) > MAX_ROWS_SHOWN:
            summary.append(f"Showing the first {MAX_ROWS_SHOWN} of {len(rows)} rows.")
    warnings = ["The SQL was written by the model; verify it answers the question."] if generated else []
    return AgentResult(
        summary="\n\n".join(summary),
        findings=[f"Columns: {', '.join(result['columns'])}."] if result["columns"] else [],
        artifacts=[{"type": "sql_result", "sql": sql, "columns": result["columns"], "rows": rows[:200]}],
        warnings=warnings,
        metadata={"tool_success": True, "row_count": result["row_count"], "generated_sql": generated},
    )
