from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile

from app.models import AgentName, ChatRequest, UploadedFile
from app.orchestrator.langgraph_flow import run_orchestrator

router = APIRouter(tags=["chat"])

UPLOAD_DIR = Path("storage/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/chat")
async def chat(
    message: str = Form(...),
    deep_research: bool = Form(False),
    agent_override: AgentName = Form(AgentName.AUTO),
    session_id: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
):
    uploaded_files: list[UploadedFile] = []

    for incoming in files:
        safe_name = Path(incoming.filename or "upload").name
        stored_name = f"{uuid4().hex}_{safe_name}"
        storage_path = UPLOAD_DIR / stored_name
        storage_path.write_bytes(await incoming.read())
        uploaded_files.append(
            UploadedFile(
                name=safe_name,
                content_type=incoming.content_type,
                storage_path=str(storage_path),
            )
        )

    request = ChatRequest(
        message=message,
        deep_research=deep_research,
        agent_override=agent_override,
        session_id=session_id,
        files=uploaded_files,
    )
    return await run_orchestrator(request)
