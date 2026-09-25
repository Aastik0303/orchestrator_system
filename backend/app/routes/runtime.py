from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.agents.catalog import load_agents
from app.config import get_settings
from app.mcp.registry import mcp_registry
from app.models import ChatRequest
from app.rag.service import EmbeddingUnavailable, embedding_service, index_document, search_knowledge
from app.security.auth import Principal, get_principal
from app.security.rate_limit import enforce_active_run_limit, enforce_rate_limit
from app.services.runtime_store import runtime_store, utc_now
from app.workers.queue import get_run_queue

router = APIRouter(tags=["runtime"])


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    project_id: str = "default"
    top_k: int = Field(default=5, ge=1, le=20)
    document_ids: list[str] = Field(default_factory=list, max_length=50)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    project_id: str = "default"
    limit: int = Field(default=6, ge=1, le=20)


class WorkflowWriteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]
    project_id: str = "default"


class WorkflowRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    project_id: str = "default"
    session_id: str | None = None
    approved_tools: list[str] = Field(default_factory=list, max_length=10)


def _embedding_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"Embedding backend unavailable: {exc}")


# ------------------------------------------------------------ capabilities


@router.get("/capabilities/agents")
async def agent_capabilities(project_id: str = "default", principal: Principal = Depends(get_principal)):
    settings = get_settings()
    registry = load_agents()
    runs = await asyncio.to_thread(runtime_store.list_runs, user_id=principal.user_id, project_id=project_id, limit=500)
    agents = []
    for spec in registry.all():
        matching = [
            run
            for run in runs
            if run.get("active_agent") == spec.name
            or any(step.get("agent") == spec.name for step in (run.get("plan") or {}).get("steps", []))
        ]
        completed = [run for run in matching if run.get("status") == "completed"]
        description = spec.describe()
        agents.append(
            {
                "id": spec.name,
                "name": spec.name.replace("_", " ").title(),
                "category": spec.category,
                "description": spec.description,
                "status": description["status"],
                "model": _model_label(spec, settings),
                "capabilities": spec.capabilities,
                "tools": spec.tools,
                "timeout_seconds": spec.timeout_seconds,
                "retry_policy": description["retry_policy"],
                "token_budget": spec.token_budget,
                "permissions": description["permissions"],
                "memory_access": "read" if "memory:read" in spec.permission_policy.granted_permissions else "none",
                "user_selectable": spec.user_selectable and spec.category == "task",
                "run_count": len(matching),
                "success_rate": round(len(completed) * 100 / len(matching), 1) if matching else None,
                "average_latency_ms": round(sum(run.get("duration_ms", 0) for run in matching) / len(matching)) if matching else None,
            }
        )
    return {"agents": agents, "capabilities": registry.capabilities()}


def _model_label(spec, settings) -> str:
    tier = spec.model_policy.tier
    if tier == "none":
        return "deterministic (no LLM)"
    if settings.llm_provider.lower() == "fake":
        return f"fake-{tier}"
    return settings.groq_model_fast if tier == "fast" else settings.groq_model


@router.get("/capabilities/mcp")
def mcp_capabilities(principal: Principal = Depends(get_principal)):
    return mcp_registry.capabilities()


@router.post("/capabilities/mcp/{server_id}/test")
def test_mcp_connection(server_id: str, principal: Principal = Depends(get_principal)):
    server = next((item for item in mcp_registry.capabilities()["servers"] if item["id"] == server_id), None)
    if not server:
        raise HTTPException(status_code=404, detail="MCP server was not found.")
    return {"id": server_id, "status": server["status"], "tested_at": utc_now()}


# -------------------------------------------------------------------- runs


@router.get("/runs")
async def list_runs(project_id: str = "default", principal: Principal = Depends(get_principal)):
    return {"runs": await asyncio.to_thread(runtime_store.list_runs, user_id=principal.user_id, project_id=project_id)}


async def _owned_run(run_id: str, principal: Principal) -> dict[str, Any]:
    run = await asyncio.to_thread(runtime_store.get_run, run_id, user_id=principal.user_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run was not found.")
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: str, principal: Principal = Depends(get_principal)):
    run = await _owned_run(run_id, principal)
    run["events"], run["steps"], run["evaluation"] = await asyncio.gather(
        asyncio.to_thread(runtime_store.list_events, run_id),
        asyncio.to_thread(runtime_store.list_steps, run_id),
        asyncio.to_thread(runtime_store.get_evaluation, run_id, user_id=principal.user_id),
    )
    return run


@router.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str, principal: Principal = Depends(get_principal)):
    await _owned_run(run_id, principal)
    return {"steps": await asyncio.to_thread(runtime_store.list_steps, run_id)}


@router.get("/runs/{run_id}/trace")
async def get_run_trace(run_id: str, principal: Principal = Depends(get_principal)):
    run = await _owned_run(run_id, principal)
    spans = await asyncio.to_thread(runtime_store.list_spans, run_id)
    return {"run_id": run_id, "trace_id": run.get("trace_id"), "metrics": run.get("metrics"), "spans": spans}


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, principal: Principal = Depends(get_principal)):
    await _owned_run(run_id, principal)
    status = await get_run_queue().cancel(run_id, user_id=principal.user_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Run was not found.")
    if status not in {"cancelled", "cancelling"}:
        raise HTTPException(status_code=409, detail=f"Run is not active (status: {status}).")
    if status == "cancelled":
        await asyncio.to_thread(
            runtime_store.append_event,
            run_id,
            "workflow_cancelled",
            node_id="final",
            node_type="output",
            label="Workflow Cancelled",
            status="cancelled",
            details={"depends_on": []},
        )
    return {"run_id": run_id, "status": status}


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry_run(run_id: str, request: Request, principal: Principal = Depends(get_principal)):
    previous = await _owned_run(run_id, principal)
    return await _start_workflow_request(
        WorkflowRunRequest(message=previous["task"], project_id=previous["project_id"], session_id=previous["session_id"]),
        principal,
        request,
    )


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, after: int = 0, principal: Principal = Depends(get_principal)):
    await _owned_run(run_id, principal)
    return {"events": await asyncio.to_thread(runtime_store.list_events, run_id, after)}


@router.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str, principal: Principal = Depends(get_principal)):
    await _owned_run(run_id, principal)
    return {"logs": await asyncio.to_thread(runtime_store.list_events, run_id)}


@router.get("/runs/{run_id}/evaluation")
async def get_run_evaluation(run_id: str, principal: Principal = Depends(get_principal)):
    evaluation = await asyncio.to_thread(runtime_store.get_evaluation, run_id, user_id=principal.user_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation was not found.")
    return evaluation


@router.get("/metrics/latency")
async def latency_metrics(run_limit: int = 200, principal: Principal = Depends(get_principal)):
    """Top latency contributors across the caller's recent runs."""
    summary = await asyncio.to_thread(
        runtime_store.latency_summary, user_id=principal.user_id, run_limit=max(1, min(run_limit, 1000))
    )
    summary["top_contributors"] = summary["contributors"][:10]
    return summary


# --------------------------------------------------------------- documents


@router.post("/documents/{document_id}/index")
@router.post("/documents/{document_id}/reindex")
async def reindex_document(document_id: str, principal: Principal = Depends(get_principal)):
    try:
        document = await asyncio.to_thread(index_document, document_id, user_id=principal.user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EmbeddingUnavailable as exc:
        raise _embedding_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {key: value for key, value in document.items() if key != "storage_path"}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, principal: Principal = Depends(get_principal)):
    document = await asyncio.to_thread(runtime_store.delete_document, document_id, user_id=principal.user_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document was not found.")
    path = Path(document["storage_path"]).resolve()
    upload_root = get_settings().uploads_dir.resolve()
    if path.is_file() and upload_root in path.parents:
        await asyncio.to_thread(path.unlink)
    return {"deleted": True, "document_id": document_id}


@router.post("/knowledge/search")
async def knowledge_search(request: KnowledgeSearchRequest, principal: Principal = Depends(get_principal)):
    enforce_rate_limit(principal.user_id, scope="search")
    try:
        results = await asyncio.to_thread(
            search_knowledge,
            request.query,
            user_id=principal.user_id,
            project_id=request.project_id,
            top_k=request.top_k,
            document_ids=request.document_ids or None,
        )
    except EmbeddingUnavailable as exc:
        raise _embedding_error(exc) from exc
    return {"results": results}


@router.get("/memory")
async def list_memory(project_id: str = "default", principal: Principal = Depends(get_principal)):
    memories = await asyncio.to_thread(runtime_store.list_memories, user_id=principal.user_id, project_id=project_id)
    for memory in memories:
        memory.pop("embedding", None)
    return {"memories": memories}


@router.post("/memory/search")
async def search_memory(request: MemorySearchRequest, principal: Principal = Depends(get_principal)):
    try:
        vector = await asyncio.to_thread(embedding_service.embed, request.query)
    except EmbeddingUnavailable as exc:
        raise _embedding_error(exc) from exc
    memories = await asyncio.to_thread(
        runtime_store.search_memories, vector, user_id=principal.user_id, project_id=request.project_id, limit=request.limit
    )
    return {"memories": memories}


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str, principal: Principal = Depends(get_principal)):
    if not await asyncio.to_thread(runtime_store.delete_memory, memory_id, user_id=principal.user_id):
        raise HTTPException(status_code=404, detail="Memory was not found.")
    return {"deleted": True, "memory_id": memory_id}


# ------------------------------------------------------ reports & evaluations


@router.get("/reports")
async def list_reports(project_id: str = "default", principal: Principal = Depends(get_principal)):
    return {"reports": await asyncio.to_thread(runtime_store.list_reports, user_id=principal.user_id, project_id=project_id)}


@router.get("/reports/{report_id}")
async def get_report(report_id: str, principal: Principal = Depends(get_principal)):
    report = await asyncio.to_thread(runtime_store.get_report, report_id, user_id=principal.user_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report was not found.")
    return report


@router.get("/evaluations")
async def list_evaluations(project_id: str = "default", principal: Principal = Depends(get_principal)):
    return {
        "evaluations": await asyncio.to_thread(
            runtime_store.list_evaluations, user_id=principal.user_id, project_id=project_id
        )
    }


@router.get("/evaluations/suites/latest")
async def latest_eval_suite(principal: Principal = Depends(get_principal)):
    """Latest offline evaluation suite report (written by `python -m evals.run`)."""
    import json

    path = Path(__file__).resolve().parents[2] / "evals" / "results" / "latest.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No evaluation suite results yet. Run `python -m evals.run`.")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/evaluations/{evaluation_id}")
async def get_evaluation(evaluation_id: str, principal: Principal = Depends(get_principal)):
    evaluation = await asyncio.to_thread(runtime_store.get_evaluation_by_id, evaluation_id, user_id=principal.user_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="Evaluation was not found.")
    return evaluation


# --------------------------------------------------------------- workflows


@router.post("/workflows", status_code=201)
async def create_workflow(request: WorkflowWriteRequest, principal: Principal = Depends(get_principal)):
    return await asyncio.to_thread(runtime_store.save_workflow, user_id=principal.user_id, **request.model_dump())


@router.get("/workflows")
async def list_workflows(principal: Principal = Depends(get_principal)):
    return {"workflows": await asyncio.to_thread(runtime_store.list_workflows, user_id=principal.user_id)}


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, principal: Principal = Depends(get_principal)):
    workflow = await asyncio.to_thread(runtime_store.get_workflow, workflow_id, user_id=principal.user_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow was not found.")
    return workflow


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, request: WorkflowWriteRequest, principal: Principal = Depends(get_principal)):
    if not await asyncio.to_thread(runtime_store.get_workflow, workflow_id, user_id=principal.user_id):
        raise HTTPException(status_code=404, detail="Workflow was not found.")
    return await asyncio.to_thread(
        runtime_store.save_workflow, workflow_id=workflow_id, user_id=principal.user_id, **request.model_dump()
    )


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, principal: Principal = Depends(get_principal)):
    if not await asyncio.to_thread(runtime_store.delete_workflow, workflow_id, user_id=principal.user_id):
        raise HTTPException(status_code=404, detail="Workflow was not found.")
    return {"deleted": True, "workflow_id": workflow_id}


@router.post("/workflows/{workflow_id}/run", status_code=202)
async def run_workflow(
    workflow_id: str, body: WorkflowRunRequest, request: Request, principal: Principal = Depends(get_principal)
):
    if not await asyncio.to_thread(runtime_store.get_workflow, workflow_id, user_id=principal.user_id):
        raise HTTPException(status_code=404, detail="Workflow was not found.")
    return await _start_workflow_request(body, principal, request)


async def _start_workflow_request(body: WorkflowRunRequest, principal: Principal, request: Request) -> dict[str, Any]:
    enforce_rate_limit(principal.user_id)
    await enforce_active_run_limit(principal.user_id)
    run_id = f"run_{uuid4().hex[:12]}"
    chat_request = ChatRequest(
        message=body.message,
        user_id=principal.user_id,
        project_id=body.project_id,
        session_id=body.session_id,
        approved_tools=body.approved_tools,
    )
    await get_run_queue().submit(run_id, chat_request, trace_id=getattr(request.state, "trace_id", None))
    return {"run_id": run_id, "status": "queued"}
