from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.config import get_settings
from app.models import AgentName, ChatRequest, UploadedFile
from app.orchestrator.langgraph_flow import run_orchestrator
from app.rag.service import index_document
from app.security.auth import Principal, get_principal, principal_from_request
from app.security.rate_limit import enforce_active_run_limit, enforce_rate_limit
from app.services.chat_store import add_message, create_session, delete_session, ensure_session, list_messages, list_sessions
from app.services.runtime_store import runtime_store
from app.workers.queue import get_run_queue

router = APIRouter(tags=["chat"])

# Magic numbers for binary formats; text formats are checked for NUL bytes.
BINARY_SIGNATURES = {".pdf": (b"%PDF",), ".docx": (b"PK\x03\x04",), ".xlsx": (b"PK\x03\x04",), ".xls": (b"\xd0\xcf\x11\xe0",)}
TEXT_EXTENSIONS = {".txt", ".md", ".csv"}


def _validate_upload(filename: str, size: int, content: bytes | None = None) -> None:
    settings = get_settings()
    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_upload_extensions:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {suffix or 'none'}.")
    if size > settings.max_upload_size:
        raise HTTPException(status_code=413, detail="File exceeds the maximum upload size.")
    if content is None:
        return
    signatures = BINARY_SIGNATURES.get(suffix)
    if signatures and not any(content.startswith(signature) for signature in signatures):
        raise HTTPException(status_code=415, detail=f"File content does not match its {suffix} extension.")
    if suffix in TEXT_EXTENSIONS and b"\x00" in content[:8192]:
        raise HTTPException(status_code=415, detail="Text uploads must not contain binary data.")


async def _store_uploads(files: list[UploadFile], *, user_id: str, project_id: str) -> list[UploadedFile]:
    settings = get_settings()
    if len(files) > settings.max_files_per_request:
        raise HTTPException(status_code=413, detail=f"At most {settings.max_files_per_request} files per request.")
    uploads_dir = settings.uploads_dir
    uploads_dir.mkdir(parents=True, exist_ok=True)
    uploaded_files: list[UploadedFile] = []
    for incoming in files:
        safe_name = Path(incoming.filename or "upload").name[:200] or "upload"
        content = await incoming.read(settings.max_upload_size + 1)
        _validate_upload(safe_name, len(content), content)
        storage_path = uploads_dir / f"{uuid4().hex}_{safe_name}"
        await asyncio.to_thread(storage_path.write_bytes, content)
        document = await asyncio.to_thread(
            runtime_store.create_document,
            user_id=user_id,
            project_id=project_id,
            name=safe_name,
            content_type=incoming.content_type,
            storage_path=str(storage_path),
            size=len(content),
        )
        try:
            await asyncio.to_thread(index_document, document["id"], user_id=user_id)
        except Exception:
            # The document row records the failed state and error for the UI.
            pass
        uploaded_files.append(
            UploadedFile(
                name=safe_name,
                content_type=incoming.content_type,
                storage_path=str(storage_path),
                document_id=document["id"],
            )
        )
    return uploaded_files


def _validate_agent_override(agent_override: str) -> None:
    from app.agents.catalog import load_agents

    if not agent_override or agent_override == AgentName.AUTO.value:
        return
    registry = load_agents()
    if not registry.has(agent_override):
        raise HTTPException(status_code=422, detail=f"Unknown agent: {agent_override}.")
    spec = registry.get(agent_override)
    if spec.category != "task" or not spec.user_selectable:
        raise HTTPException(status_code=422, detail="System agents cannot be selected manually.")


def _approved_tools(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()][:10]


async def _prepare_request(
    *,
    principal: Principal,
    message: str,
    deep_research: bool,
    agent_override: str,
    session_id: str | None,
    project_id: str,
    files: list[UploadFile],
    approved_tools: str | None,
) -> tuple[ChatRequest, str]:
    enforce_rate_limit(principal.user_id)
    _validate_agent_override(agent_override)
    if not message.strip():
        raise HTTPException(status_code=422, detail="Message must not be empty.")
    if len(message) > 50_000:
        raise HTTPException(status_code=413, detail="Message is too long.")
    session = await asyncio.to_thread(ensure_session, session_id, message[:80], user_id=principal.user_id)
    await asyncio.to_thread(add_message, session["id"], "user", message)
    uploaded_files = await _store_uploads(files, user_id=principal.user_id, project_id=project_id)
    return (
        ChatRequest(
            message=message,
            deep_research=deep_research,
            agent_override=agent_override or AgentName.AUTO.value,
            session_id=session["id"],
            user_id=principal.user_id,
            project_id=project_id,
            files=uploaded_files,
            approved_tools=_approved_tools(approved_tools),
        ),
        session["id"],
    )


def _chat_principal(request: Request, user_id: str | None) -> Principal:
    return principal_from_request(request, user_id)


@router.post("/chat")
async def chat(
    request: Request,
    message: str = Form(...),
    deep_research: bool = Form(False),
    agent_override: str = Form(AgentName.AUTO.value),
    session_id: str | None = Form(None),
    user_id: str | None = Form(None),
    project_id: str = Form("default"),
    approved_tools: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
):
    principal = _chat_principal(request, user_id)
    chat_request, resolved_session_id = await _prepare_request(
        principal=principal,
        message=message,
        deep_research=deep_research,
        agent_override=agent_override,
        session_id=session_id,
        project_id=project_id,
        files=files,
        approved_tools=approved_tools,
    )
    run_id = f"run_{uuid4().hex[:12]}"
    try:
        response = await run_orchestrator(chat_request, run_id=run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(add_message, resolved_session_id, "assistant", response.response)
    return {**response.model_dump(mode="json"), "run_id": run_id, "session_id": resolved_session_id}


@router.post("/chat/start", status_code=202)
async def start_chat(
    request: Request,
    message: str = Form(...),
    deep_research: bool = Form(False),
    agent_override: str = Form(AgentName.AUTO.value),
    session_id: str | None = Form(None),
    user_id: str | None = Form(None),
    project_id: str = Form("default"),
    approved_tools: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
):
    principal = _chat_principal(request, user_id)
    await enforce_active_run_limit(principal.user_id)
    chat_request, resolved_session_id = await _prepare_request(
        principal=principal,
        message=message,
        deep_research=deep_research,
        agent_override=agent_override,
        session_id=session_id,
        project_id=project_id,
        files=files,
        approved_tools=approved_tools,
    )
    run_id = f"run_{uuid4().hex[:12]}"
    await get_run_queue().submit(run_id, chat_request, trace_id=getattr(request.state, "trace_id", None))
    return {"run_id": run_id, "session_id": resolved_session_id, "status": "queued"}


@router.get("/chat/sessions")
async def chat_sessions(principal: Principal = Depends(get_principal)):
    return {"sessions": await asyncio.to_thread(list_sessions, user_id=principal.user_id)}


@router.post("/chat/sessions")
async def new_chat_session(principal: Principal = Depends(get_principal)):
    return {"session": await asyncio.to_thread(create_session, user_id=principal.user_id)}


@router.get("/chat/sessions/{session_id}/messages")
async def chat_session_messages(session_id: str, principal: Principal = Depends(get_principal)):
    return {"messages": await asyncio.to_thread(list_messages, session_id, user_id=principal.user_id)}


@router.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, principal: Principal = Depends(get_principal)):
    if not await asyncio.to_thread(delete_session, session_id, user_id=principal.user_id):
        raise HTTPException(status_code=404, detail="Chat session was not found.")
    return {"deleted": True, "session_id": session_id}


@router.post("/documents/upload")
async def upload_documents(
    request: Request,
    files: list[UploadFile] = File(default=[]),
    user_id: str | None = Form(None),
    project_id: str = Form("default"),
):
    principal = principal_from_request(request, user_id)
    enforce_rate_limit(principal.user_id, scope="upload")
    uploaded = await _store_uploads(files, user_id=principal.user_id, project_id=project_id)
    documents = [
        await asyncio.to_thread(runtime_store.get_document, file.document_id or "", user_id=principal.user_id)
        for file in uploaded
    ]
    return {"documents": [_public_document(document) for document in documents if document]}


def _public_document(document: dict) -> dict:
    """Never expose server filesystem paths to clients."""
    return {key: value for key, value in document.items() if key != "storage_path"}


@router.get("/runtime")
async def runtime_snapshot(project_id: str = "default", principal: Principal = Depends(get_principal)):
    user_id = principal.user_id
    runs, documents, reports, evaluations = await asyncio.gather(
        asyncio.to_thread(runtime_store.list_runs, user_id=user_id, project_id=project_id),
        asyncio.to_thread(runtime_store.list_documents, user_id=user_id, project_id=project_id),
        asyncio.to_thread(runtime_store.list_reports, user_id=user_id, project_id=project_id),
        asyncio.to_thread(runtime_store.list_evaluations, user_id=user_id, project_id=project_id),
    )
    completed = [run for run in runs if run["status"] == "completed"]
    failed = [run for run in runs if run["status"] in {"failed", "timeout"}]
    blocked = [run for run in runs if run["status"] == "blocked"]
    total_duration = sum(run["duration_ms"] for run in runs)
    scores = [evaluation["overall_score"] for evaluation in evaluations]
    return {
        "health": "ok",
        "runs": runs,
        "documents": [_public_document(document) for document in documents],
        "reports": reports,
        "evaluation": {
            "available": bool(runs),
            "successful_runs": len(completed),
            "failed_runs": len(failed),
            "blocked_runs": len(blocked),
            "success_rate": round(len(completed) * 100 / len(runs), 1) if runs else 0,
            "average_latency_ms": round(total_duration / len(runs)) if runs else 0,
            "average_quality_score": round(sum(scores) / len(scores), 1) if scores else None,
        },
    }
