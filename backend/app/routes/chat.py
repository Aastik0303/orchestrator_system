from pathlib import Path
from time import perf_counter
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile

from app.models import AgentName, ChatRequest, UploadedFile
from app.orchestrator.langgraph_flow import run_orchestrator
from app.orchestrator.supervisor import AGENTS
from app.services.chat_store import add_message, create_session, ensure_session, list_messages, list_sessions

router = APIRouter(tags=["chat"])

UPLOAD_DIR = Path("storage/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

RUNTIME_RUNS: list[dict] = []
RUNTIME_DOCUMENTS: list[dict] = []


def _document_record(file: UploadedFile, size: int) -> dict:
    return {
        "id": uuid4().hex,
        "name": file.name,
        "content_type": file.content_type,
        "storage_path": file.storage_path,
        "size": size,
        "status": "indexed",
        "chunk_count": max(1, size // 1800 + 1),
    }


async def _store_uploads(files: list[UploadFile]) -> list[tuple[UploadedFile, int]]:
    uploaded_files: list[tuple[UploadedFile, int]] = []

    for incoming in files:
        safe_name = Path(incoming.filename or "upload").name
        stored_name = f"{uuid4().hex}_{safe_name}"
        storage_path = UPLOAD_DIR / stored_name
        content = await incoming.read()
        storage_path.write_bytes(content)
        uploaded_files.append(
            (
                UploadedFile(
                    name=safe_name,
                    content_type=incoming.content_type,
                    storage_path=str(storage_path),
                ),
                len(content),
            )
        )

    return uploaded_files


@router.post("/chat")
async def chat(
    message: str = Form(...),
    deep_research: bool = Form(False),
    agent_override: AgentName = Form(AgentName.AUTO),
    session_id: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
):
    started = perf_counter()
    run_id = f"run_{uuid4().hex[:12]}"
    session = ensure_session(session_id, message[:80])
    add_message(session["id"], "user", message)
    stored_files = await _store_uploads(files)
    uploaded_files = [file for file, _size in stored_files]
    documents = [_document_record(file, size) for file, size in stored_files]
    RUNTIME_DOCUMENTS[:0] = documents

    request = ChatRequest(
        message=message,
        deep_research=deep_research,
        agent_override=agent_override,
        session_id=session["id"],
        files=uploaded_files,
    )
    response = await run_orchestrator(request)
    duration_ms = int((perf_counter() - started) * 1000)
    failed = response.response.startswith("I could not complete")
    RUNTIME_RUNS.insert(
        0,
        {
            "id": run_id,
            "task": message,
            "status": "failed" if failed else "completed",
            "active_agent": response.active_agent.value,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "file_count": len(uploaded_files),
            "document_ids": [document["id"] for document in documents],
            "response": response.response,
            "artifacts": response.artifacts,
            "needs_clarification": response.needs_clarification,
        },
    )
    del RUNTIME_RUNS[50:]
    add_message(session["id"], "assistant", response.response)
    return {
        **response.model_dump(mode="json"),
        "session_id": session["id"],
    }


@router.get("/chat/sessions")
def chat_sessions():
    return {"sessions": list_sessions()}


@router.post("/chat/sessions")
def new_chat_session():
    return {"session": create_session()}


@router.get("/chat/sessions/{session_id}/messages")
def chat_session_messages(session_id: str):
    return {"messages": list_messages(session_id)}


@router.post("/documents/upload")
async def upload_documents(files: list[UploadFile] = File(default=[])):
    stored_files = await _store_uploads(files)
    documents = [_document_record(file, size) for file, size in stored_files]
    RUNTIME_DOCUMENTS[:0] = documents
    return {"documents": documents}


@router.get("/runtime")
def runtime_snapshot():
    completed_runs = [run for run in RUNTIME_RUNS if run["status"] == "completed"]
    failed_runs = [run for run in RUNTIME_RUNS if run["status"] == "failed"]
    total_duration = sum(run["duration_ms"] for run in RUNTIME_RUNS)
    return {
        "health": "ok",
        "runs": RUNTIME_RUNS,
        "documents": RUNTIME_DOCUMENTS,
        "reports": [
            {
                "id": f"report_{run['id']}",
                "run_id": run["id"],
                "title": run["task"][:80],
                "format": "text",
                "created_at": run["started_at"],
                "status": run["status"],
                "active_agent": run["active_agent"],
                "content": run["response"],
            }
            for run in completed_runs
        ],
        "evaluation": {
            "available": bool(RUNTIME_RUNS),
            "successful_runs": len(completed_runs),
            "failed_runs": len(failed_runs),
            "success_rate": round((len(completed_runs) / len(RUNTIME_RUNS)) * 100, 1) if RUNTIME_RUNS else 0,
            "average_latency_ms": round(total_duration / len(RUNTIME_RUNS)) if RUNTIME_RUNS else 0,
        },
        "capabilities": [
            {
                "id": agent.value,
                "name": agent.value.replace("_", " ").title(),
                "status": "available",
            }
            for agent in AGENTS
        ],
    }
