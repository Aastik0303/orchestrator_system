"""Tool Registry.

Every tool declares: name, description, required permissions, timeout, risk
level, input schema (pydantic model) and output schema. Execution:

  permission guard -> input validation -> handler (thread, timeout) -> stats

File-backed tools take document ids and resolve them through the caller's
ownership (never raw paths), so a tool can only touch the calling user's data.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import pandas as pd
from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.core.errors import InvalidInputError, StepTimeoutError, ToolError
from app.mcp.permissions import enforce_permission
from app.mcp.schemas import RiskLevel, ToolContext, ToolDefinition, ToolPermission
from app.observability.telemetry import current_telemetry
from app.services.runtime_store import runtime_store, utc_now

if TYPE_CHECKING:
    from app.agents.registry import AgentSpec

ToolHandler = Callable[[Any, ToolContext], Any | Awaitable[Any]]


class MCPRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}
        # Per-process observability counters (not used for any decision).
        self._stats: dict[str, dict[str, Any]] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler | None = None) -> None:
        qualified_name = definition.qualified_name
        if handler is None and definition.status == "available":
            definition.status = "not_implemented"
        self._tools[qualified_name] = definition
        if handler:
            self._handlers[qualified_name] = handler
        self._stats.setdefault(
            qualified_name,
            {"calls": 0, "successes": 0, "total_latency_ms": 0, "last_used": None},
        )

    def get(self, tool_name: str) -> ToolDefinition | None:
        return self._tools.get(tool_name)

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        agent: "AgentSpec | None" = None,
    ) -> dict[str, Any]:
        tool = self._tools.get(tool_name)
        if not tool:
            raise InvalidInputError(f"Unknown tool: {tool_name}")
        enforce_permission(tool, arguments, context, agent)
        if tool.status != "available" or tool_name not in self._handlers:
            raise ToolError(f"Tool is not configured: {tool_name}", retryable=False)
        parsed: Any = arguments
        if tool.input_model is not None:
            try:
                parsed = tool.input_model.model_validate(arguments)
            except ValidationError as exc:
                raise InvalidInputError(
                    f"Invalid arguments for {tool_name}: {exc.errors()[0].get('msg', 'invalid')}"
                ) from None

        stats = self._stats[tool_name]
        stats["calls"] += 1
        started = perf_counter()
        telemetry = current_telemetry.get()
        span_cm = telemetry.span("tool", tool_name, risk=tool.risk_level.value, agent=context.agent) if telemetry else None
        try:
            if span_cm is not None:
                async with span_cm:
                    result = await self._invoke(tool, parsed, context)
            else:
                result = await self._invoke(tool, parsed, context)
            stats["successes"] += 1
            return {"tool": tool_name, "status": "completed", "result": result}
        finally:
            stats["total_latency_ms"] += int((perf_counter() - started) * 1000)
            stats["last_used"] = utc_now()

    async def _invoke(self, tool: ToolDefinition, parsed: Any, context: ToolContext) -> Any:
        handler = self._handlers[tool.qualified_name]
        try:
            if asyncio.iscoroutinefunction(handler):
                return await asyncio.wait_for(handler(parsed, context), timeout=tool.timeout_seconds)
            return await asyncio.wait_for(
                asyncio.to_thread(handler, parsed, context), timeout=tool.timeout_seconds
            )
        except asyncio.TimeoutError:
            raise StepTimeoutError(f"Tool {tool.qualified_name} timed out after {tool.timeout_seconds}s.") from None
        except (PermissionError, InvalidInputError, ToolError, StepTimeoutError):
            raise
        except (FileNotFoundError, ValueError, KeyError) as exc:
            raise InvalidInputError(f"{tool.qualified_name}: {exc}") from None
        except Exception as exc:
            raise ToolError(f"{tool.qualified_name} failed ({exc.__class__.__name__}).") from None

    def tool_names(self) -> set[str]:
        return set(self._tools)

    def capabilities(self) -> dict[str, Any]:
        servers: dict[str, dict[str, Any]] = {}
        server_names = {
            "github": "GitHub MCP",
            "file": "File MCP",
            "vector": "Vector MCP",
            "web": "Web MCP",
            "data": "Data MCP",
            "sandbox": "Sandbox",
        }
        for qualified_name, tool in self._tools.items():
            stats = self._stats[qualified_name]
            server = servers.setdefault(
                tool.server,
                {
                    "id": tool.server,
                    "name": server_names.get(tool.server, tool.server.title()),
                    "status": "connected",
                    "tools": [],
                },
            )
            calls = stats["calls"]
            server["tools"].append(
                {
                    "name": tool.name,
                    "qualified_name": qualified_name,
                    "description": tool.description,
                    "permission": tool.permission.value,
                    "risk_level": tool.risk_level.value,
                    "required_permissions": tool.required_permissions,
                    "timeout_seconds": tool.timeout_seconds,
                    "requires_approval": tool.requires_approval,
                    "read_only": tool.read_only,
                    "input_schema": tool.input_schema,
                    "output_schema": tool.output_schema,
                    "status": tool.status,
                    "average_latency_ms": round(stats["total_latency_ms"] / calls) if calls else None,
                    "success_rate": round(stats["successes"] * 100 / calls, 1) if calls else None,
                    "last_used": stats["last_used"],
                }
            )
        for server in servers.values():
            statuses = {tool["status"] for tool in server["tools"]}
            if statuses == {"available"}:
                server["status"] = "connected"
            elif "available" in statuses:
                server["status"] = "partial"
            elif "not_configured" in statuses:
                server["status"] = "not_configured"
            else:
                server["status"] = "not_implemented"
        return {"servers": list(servers.values())}


# ------------------------------------------------------------ input models


class DocumentRef(BaseModel):
    document_id: str = Field(min_length=1, max_length=64)


class WriteReportInput(BaseModel):
    name: str = Field(default="report.md", max_length=120)
    content: str = Field(max_length=5 * 1024 * 1024)


class VectorSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    document_ids: list[str] = Field(default_factory=list, max_length=50)


class StoreMemoryInput(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    memory_type: str = Field(default="semantic", pattern="^(semantic|preference|workflow|fact)$")
    importance_score: float = Field(default=0.5, ge=0, le=1)


class SearchMemoryInput(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=6, ge=1, le=20)


class PythonExecInput(BaseModel):
    code: str = Field(min_length=1, max_length=20_000)


class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- handlers


def _approved_roots() -> list[Path]:
    settings = get_settings()
    return [settings.uploads_dir.resolve(), settings.outputs_dir.resolve()]


def _owned_document_path(document_id: str, context: ToolContext) -> tuple[dict[str, Any], Path]:
    document = runtime_store.get_document(document_id, user_id=context.user_id)
    if not document:
        raise FileNotFoundError(f"Document {document_id} was not found.")
    path = Path(str(document.get("storage_path") or "")).resolve()
    if not path.is_file():
        raise FileNotFoundError("Stored document file is missing.")
    return document, path


def _read_file(arguments: DocumentRef, context: ToolContext) -> dict[str, Any]:
    document, path = _owned_document_path(arguments.document_id, context)
    return {"name": document["name"], "content": path.read_text(encoding="utf-8", errors="ignore")[:100_000]}


def _list_files(arguments: Any, context: ToolContext) -> list[dict[str, Any]]:
    return [
        {"document_id": item["id"], "name": item["name"], "size": item["size"], "status": item["status"]}
        for item in runtime_store.list_documents(user_id=context.user_id, project_id=context.project_id)
    ]


def _extract_text(arguments: DocumentRef, context: ToolContext) -> dict[str, Any]:
    from app.rag.service import extract_document

    document, path = _owned_document_path(arguments.document_id, context)
    pages = extract_document(path)
    return {"name": document["name"], "pages": [{"page": page, "text": text} for page, text in pages]}


def _write_report(arguments: WriteReportInput, context: ToolContext) -> dict[str, Any]:
    name = Path(arguments.name).name
    if Path(name).suffix.lower() not in {".md", ".html", ".json"}:
        raise ValueError("Report output must be Markdown, HTML, or JSON.")
    owner = hashlib.sha256(context.user_id.encode()).hexdigest()[:16]
    directory = get_settings().outputs_dir / owner
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / name).resolve()
    if not any(root in path.parents for root in _approved_roots()):
        raise PermissionError("Report path is outside approved storage roots.")
    path.write_text(arguments.content, encoding="utf-8")
    return {"name": name, "path": str(path), "size": path.stat().st_size}


def _vector_index(arguments: DocumentRef, context: ToolContext) -> dict[str, Any]:
    from app.rag.service import index_document

    return index_document(arguments.document_id, user_id=context.user_id)


def _vector_search(arguments: VectorSearchInput, context: ToolContext) -> list[dict[str, Any]]:
    from app.rag.service import search_knowledge

    return search_knowledge(
        arguments.query,
        user_id=context.user_id,
        project_id=context.project_id,
        top_k=arguments.top_k,
        document_ids=arguments.document_ids or None,
    )


def _vector_delete(arguments: DocumentRef, context: ToolContext) -> dict[str, Any]:
    document = runtime_store.delete_document(arguments.document_id, user_id=context.user_id)
    if not document:
        raise FileNotFoundError(arguments.document_id)
    return {"deleted": True, "document_id": arguments.document_id}


def _store_memory(arguments: StoreMemoryInput, context: ToolContext) -> dict[str, Any]:
    from app.memory.service import save_memory_safely

    saved = save_memory_safely(
        arguments.content,
        user_id=context.user_id,
        project_id=context.project_id,
        memory_type=arguments.memory_type,
        importance_score=arguments.importance_score,
        run_id=context.run_id,
        session_id=None,
    )
    if saved is None:
        return {"stored": False, "reason": "Content was empty or contained sensitive data."}
    return {"stored": True, "memory_id": saved["id"]}


def _search_memory(arguments: SearchMemoryInput, context: ToolContext) -> list[dict[str, Any]]:
    from app.memory.service import retrieve_memory

    return retrieve_memory(
        arguments.query,
        user_id=context.user_id,
        project_id=context.project_id,
        limit=arguments.limit,
    )


def _load_frame(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    raise ValueError("Dataset must be CSV or Excel.")


def _inspect_dataset(arguments: DocumentRef, context: ToolContext) -> dict[str, Any]:
    _, path = _owned_document_path(arguments.document_id, context)
    frame = _load_frame(path)
    return {
        "rows": len(frame),
        "columns": [str(column) for column in frame.columns],
        "null_counts": {str(key): int(value) for key, value in frame.isna().sum().items()},
    }


def _calculate_statistics(arguments: DocumentRef, context: ToolContext) -> dict[str, Any]:
    _, path = _owned_document_path(arguments.document_id, context)
    frame = _load_frame(path)
    numeric = frame.select_dtypes(include="number")
    if numeric.empty:
        return {"columns": {}, "warning": "Dataset has no numeric columns."}
    if len(numeric.columns) > 100 or len(numeric) > 1_000_000:
        raise ValueError("Dataset exceeds the bounded statistics limit.")
    return {
        "columns": {
            str(column): {
                key: float(value) for key, value in numeric[column].describe().items() if pd.notna(value)
            }
            for column in numeric.columns
        }
    }


def _python_exec(arguments: PythonExecInput, context: ToolContext) -> dict[str, Any]:
    from app.sandbox import run_python

    settings = get_settings()
    if not settings.sandbox_enabled:
        raise PermissionError("Sandboxed execution is disabled (SANDBOX_ENABLED=false).")
    return run_python(
        arguments.code,
        timeout_seconds=settings.sandbox_timeout_seconds,
        memory_mb=settings.sandbox_memory_mb,
        max_output_bytes=settings.sandbox_max_output_bytes,
    ).to_dict()


def _register_tools() -> MCPRegistry:
    import os

    registry = MCPRegistry()
    github_status = "available" if os.getenv("GITHUB_TOKEN") else "not_configured"
    web_status = "available" if os.getenv("WEB_SEARCH_API_KEY") else "not_configured"

    for name in ("read_repository", "list_repository_files", "read_file", "search_code", "list_commits"):
        registry.register(
            ToolDefinition(
                name=name,
                server="github",
                description=f"Read-only GitHub operation: {name.replace('_', ' ')}.",
                permission=ToolPermission.ALLOWED,
                required_permissions=["github:read"],
                status=github_status,
            )
        )
    for name in ("create_issue", "create_branch", "create_pull_request"):
        registry.register(
            ToolDefinition(
                name=name,
                server="github",
                description=f"GitHub write operation: {name.replace('_', ' ')}.",
                permission=ToolPermission.APPROVAL_REQUIRED,
                risk_level=RiskLevel.HIGH,
                required_permissions=["github:write"],
                status=github_status,
                read_only=False,
            )
        )
    registry.register(ToolDefinition(name="read_file", server="file", description="Read one of the caller's uploaded text documents.", permission=ToolPermission.ALLOWED, required_permissions=["files:read"], input_model=DocumentRef), _read_file)
    registry.register(ToolDefinition(name="list_files", server="file", description="List the caller's uploaded documents.", permission=ToolPermission.ALLOWED, required_permissions=["files:read"]), _list_files)
    registry.register(ToolDefinition(name="extract_pdf_text", server="file", description="Extract page text from one of the caller's PDFs.", permission=ToolPermission.ALLOWED, required_permissions=["files:read"], input_model=DocumentRef), _extract_text)
    registry.register(ToolDefinition(name="extract_docx_text", server="file", description="Extract text from one of the caller's DOCX files.", permission=ToolPermission.ALLOWED, required_permissions=["files:read"], input_model=DocumentRef), _extract_text)
    registry.register(ToolDefinition(name="write_report", server="file", description="Write a generated report artifact to the caller's output folder.", permission=ToolPermission.APPROVAL_REQUIRED, risk_level=RiskLevel.MEDIUM, required_permissions=["files:write"], read_only=False, input_model=WriteReportInput), _write_report)
    registry.register(ToolDefinition(name="index_document", server="vector", description="Extract, chunk, embed, and index one of the caller's documents.", permission=ToolPermission.ALLOWED, risk_level=RiskLevel.MEDIUM, required_permissions=["vector:write"], read_only=False, timeout_seconds=120, input_model=DocumentRef), _vector_index)
    registry.register(ToolDefinition(name="search_chunks", server="vector", description="Search the caller's indexed chunks by vector similarity.", permission=ToolPermission.ALLOWED, required_permissions=["vector:read"], timeout_seconds=20, input_model=VectorSearchInput, output_schema={"type": "array"}), _vector_search)
    registry.register(ToolDefinition(name="delete_document", server="vector", description="Delete one of the caller's documents and its vectors.", permission=ToolPermission.APPROVAL_REQUIRED, risk_level=RiskLevel.HIGH, required_permissions=["vector:write"], read_only=False, input_model=DocumentRef), _vector_delete)
    registry.register(ToolDefinition(name="store_memory", server="vector", description="Store scoped long-term memory (sensitive content is rejected).", permission=ToolPermission.ALLOWED, required_permissions=["memory:write"], read_only=False, input_model=StoreMemoryInput), _store_memory)
    registry.register(ToolDefinition(name="search_memory", server="vector", description="Search the caller's scoped long-term memory.", permission=ToolPermission.ALLOWED, required_permissions=["memory:read"], timeout_seconds=15, input_model=SearchMemoryInput, output_schema={"type": "array"}), _search_memory)
    for name in ("web_search", "fetch_page", "extract_content"):
        registry.register(ToolDefinition(name=name, server="web", description=f"Web operation: {name.replace('_', ' ')}.", permission=ToolPermission.ALLOWED, required_permissions=["web:read"], status=web_status, input_model=WebSearchInput if name == "web_search" else None))
    registry.register(ToolDefinition(name="inspect_dataset", server="data", description="Inspect one of the caller's CSV/Excel datasets without modifying it.", permission=ToolPermission.ALLOWED, required_permissions=["files:read"], input_model=DocumentRef), _inspect_dataset)
    registry.register(ToolDefinition(name="calculate_statistics", server="data", description="Calculate bounded descriptive statistics.", permission=ToolPermission.ALLOWED, required_permissions=["files:read"], input_model=DocumentRef), _calculate_statistics)
    registry.register(ToolDefinition(name="run_safe_analysis", server="data", description="Run an allowlisted data analysis operation.", permission=ToolPermission.APPROVAL_REQUIRED, risk_level=RiskLevel.MEDIUM))
    registry.register(ToolDefinition(name="generate_chart", server="data", description="Generate a chart artifact from approved data.", permission=ToolPermission.ALLOWED, read_only=False))
    registry.register(
        ToolDefinition(
            name="python_exec",
            server="sandbox",
            description="Execute a Python snippet in an isolated, resource-limited sandbox process.",
            permission=ToolPermission.APPROVAL_REQUIRED,
            risk_level=RiskLevel.HIGH,
            required_permissions=["code:execute"],
            timeout_seconds=30,
            input_model=PythonExecInput,
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"enum": ["ok", "error", "timeout", "blocked", "memory_exceeded"]},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
        ),
        _python_exec,
    )
    registry.register(ToolDefinition(name="shell", server="sandbox", description="Host shell access. Never available to agents.", permission=ToolPermission.BLOCKED, risk_level=RiskLevel.CRITICAL, required_permissions=["host:shell"], read_only=False))
    registry.register(ToolDefinition(name="production_database", server="data", description="Direct production database access. Never available to agents.", permission=ToolPermission.BLOCKED, risk_level=RiskLevel.CRITICAL, required_permissions=["db:admin"], read_only=False))
    return registry


mcp_registry = _register_tools()
