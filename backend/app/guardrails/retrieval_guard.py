"""Retrieval guardrail and context builder.

Retrieved documents are DATA, never authority:
* chunks containing instructions aimed at the model are quarantined,
* secrets and high-risk PII inside chunks are redacted before prompting,
* the context is bounded (characters, chunks per document, near-duplicates),
* every chunk is wrapped in an explicit untrusted <document> delimiter.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.prompts import wrap_untrusted
from app.config import get_settings
from app.guardrails.detectors import RETRIEVAL_DETECTORS, redact_pii, redact_secrets, scan
from app.models import GuardrailFinding, GuardrailVerdict


def sanitize_retrieved_chunks(chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], GuardrailVerdict]:
    settings = get_settings()
    safe: list[dict[str, Any]] = []
    findings: list[GuardrailFinding] = []
    quarantined = 0
    redacted_count = 0
    for chunk in chunks:
        matches = scan(chunk.get("content", ""), RETRIEVAL_DETECTORS)
        if matches or chunk.get("prompt_injection_detected"):
            quarantined += 1
            continue
        content, secret_kinds = redact_secrets(chunk.get("content", ""), settings.secret_values())
        content, pii_kinds = redact_pii(content, strict=settings.pii_redaction.lower() == "strict")
        if secret_kinds or pii_kinds:
            redacted_count += 1
        safe.append({**chunk, "content": content})
    if quarantined:
        findings.append(
            GuardrailFinding(
                category="retrieved_prompt_injection",
                severity="high",
                detail=f"{quarantined} retrieved chunk(s) contained instructions and were excluded.",
            )
        )
    if redacted_count:
        findings.append(
            GuardrailFinding(
                category="retrieved_sensitive_data",
                severity="medium",
                detail=f"Sensitive values were redacted from {redacted_count} chunk(s).",
            )
        )
    action = "redact" if findings else "allow"
    return safe, GuardrailVerdict(stage="retrieval", action=action, findings=findings)


def _fingerprint(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower())[:40])


def build_context(
    chunks: list[dict[str, Any]],
    *,
    max_chars: int | None = None,
    max_chunks_per_document: int | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Select chunks (best first) within the context budget and render them."""
    settings = get_settings()
    budget = max_chars or settings.rag_max_context_chars
    per_document_cap = max_chunks_per_document or settings.rag_max_chunks_per_document
    per_document: dict[str, int] = {}
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    used = 0
    for chunk in sorted(chunks, key=lambda item: item.get("similarity", 0), reverse=True):
        document_id = str(chunk.get("document_id"))
        if per_document.get(document_id, 0) >= per_document_cap:
            continue
        fingerprint = _fingerprint(chunk.get("content", ""))
        if fingerprint in seen:
            continue
        content = chunk.get("content", "")
        remaining = budget - used
        if remaining < 200:
            break
        if len(content) > remaining:
            content = content[:remaining].rsplit(" ", 1)[0] + " ..."
        selected.append({**chunk, "content": content})
        seen.add(fingerprint)
        per_document[document_id] = per_document.get(document_id, 0) + 1
        used += len(content)
    rendered = "\n\n".join(
        wrap_untrusted(
            "document",
            chunk["content"],
            index=index,
            source=chunk.get("document_name"),
            page=chunk.get("page_number"),
        )
        for index, chunk in enumerate(selected, start=1)
    )
    return rendered, selected


# [Source 2] and [Source 2, 1:30] (a locator after the number is kept).
CITATION_RE = re.compile(r"\[Source\s+(\d+)(?:\s*[,;:][^\]\n]{0,40})?\]", re.IGNORECASE)
# Models trained on other citation styles write 【Source 2】 or 【Source 2, 1:30】.
ALT_CITATION_RE = re.compile(r"【\s*Source\s+(\d+)\s*((?:[,;:][^】\n]{0,40})?)】", re.IGNORECASE)
WORD_RE = re.compile(r"[a-z0-9]{4,}")


def normalize_citations(answer: str) -> str:
    return ALT_CITATION_RE.sub(lambda match: f"[Source {match.group(1)}{match.group(2).rstrip()}]", answer)


def validate_citations(answer: str, source_count: int) -> tuple[str, list[str]]:
    """Normalize citation style, remove citations to sources that do not
    exist, and report uncited answers."""
    answer = normalize_citations(answer)
    warnings: list[str] = []
    invalid = sorted({int(number) for number in CITATION_RE.findall(answer) if not 1 <= int(number) <= source_count})
    if invalid:
        answer = CITATION_RE.sub(
            lambda match: match.group(0) if 1 <= int(match.group(1)) <= source_count else "",
            answer,
        )
        warnings.append(f"Removed citations to non-existent sources: {invalid}.")
    if source_count and not CITATION_RE.search(answer):
        warnings.append("The answer does not cite the retrieved sources; verify it against the evidence.")
    return answer, warnings


def faithfulness_score(answer: str, context_chunks: list[dict[str, Any]]) -> float:
    """Share of answer sentences whose content words are mostly present in the
    retrieved context (lexical support; a cheap hallucination signal)."""
    context_terms = set(WORD_RE.findall(" ".join(chunk.get("content", "") for chunk in context_chunks).lower()))
    sentences = [sentence for sentence in re.split(r"(?<=[.!?])\s+", CITATION_RE.sub("", answer)) if WORD_RE.search(sentence.lower())]
    if not sentences:
        return 0.0
    supported = 0
    for sentence in sentences:
        terms = set(WORD_RE.findall(sentence.lower()))
        if terms and len(terms & context_terms) / len(terms) >= 0.5:
            supported += 1
    return round(supported / len(sentences), 3)
