from pathlib import Path

from docx import Document
from pypdf import PdfReader

from app.models import AgentName, AgentResponse, ChatRequest
from app.services.groq_client import groq_completion

MAX_CONTEXT_CHARS = 18000


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()

    if suffix == ".docx":
        document = Document(path)
        return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())

    if suffix == ".pdf":
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    return ""


def document_rag_agent(request: ChatRequest) -> AgentResponse:
    contexts: list[str] = []
    skipped: list[str] = []

    for uploaded in request.files:
        if not uploaded.storage_path:
            skipped.append(uploaded.name)
            continue

        path = Path(uploaded.storage_path)
        text = _extract_text(path).strip()

        if text:
            contexts.append(f"File: {uploaded.name}\n{text[:MAX_CONTEXT_CHARS]}")
        else:
            skipped.append(uploaded.name)

    if not contexts:
        return AgentResponse(
            active_agent=AgentName.DOCUMENT_RAG,
            response=(
                "Document RAG Agent active, but I could not extract readable text from the uploaded file. "
                "Try a .docx, .pdf, .txt, or .md file."
            ),
        )

    answer = groq_completion(
        (
            "You are the Document RAG Agent. Answer the user's question using only the uploaded "
            "document context. If the answer is not present, say that the document does not contain it."
        ),
        f"User question: {request.message}\n\nDocument context:\n\n{'\n\n---\n\n'.join(contexts)}",
    )

    if answer:
        return AgentResponse(active_agent=AgentName.DOCUMENT_RAG, response=answer)

    file_names = ", ".join(file.name for file in request.files)
    skipped_note = f" Skipped unreadable files: {', '.join(skipped)}." if skipped else ""
    return AgentResponse(
        active_agent=AgentName.DOCUMENT_RAG,
        response=(
            "Document RAG Agent extracted readable text from "
            f"{file_names}, but GROQ_API_KEY is not available to generate an answer."
            f"{skipped_note}"
        ),
    )
