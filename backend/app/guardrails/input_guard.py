"""Input guardrail: the first stage of the pipeline, before routing or any
model call. Blocking here means no LLM, tool or retrieval work is done."""

from __future__ import annotations

from app.config import get_settings
from app.guardrails.detectors import scan
from app.models import ChatRequest, GuardrailFinding, GuardrailVerdict

REFUSALS = {
    "credential_request": "I can't share credentials, API keys, tokens, environment variables or other secrets.",
    "system_prompt_extraction": "I can't share my internal instructions or configuration.",
    "prompt_injection": "I can't follow instructions that try to override the platform's rules.",
    "jailbreak": "I can't switch off my safety rules or adopt a mode without them.",
    "cross_user_access": "I can only access your own conversations, documents and memory.",
    "tool_abuse": "I can't bypass tool permissions, approvals or the sandbox.",
    "dangerous_execution": "I can't run shell commands or destructive system operations.",
    "malicious_instructions": "I can't help create malware or tools meant to cause harm.",
    "excessive_resource": "That request would consume unbounded resources; please narrow it down.",
    "oversized_input": "The request is too large to process; please shorten it.",
}


def refusal_message(findings: list[GuardrailFinding]) -> str:
    lines = [REFUSALS.get(finding.category, "I can't help with that request.") for finding in findings]
    unique = list(dict.fromkeys(lines))
    return " ".join(unique) + " If you have a legitimate task, rephrase it without that part and I'll help."


def inspect_input(request: ChatRequest) -> GuardrailVerdict:
    settings = get_settings()
    findings: list[GuardrailFinding] = []
    if len(request.message) > settings.max_message_chars:
        findings.append(
            GuardrailFinding(
                category="oversized_input",
                severity="medium",
                detail=f"Message exceeds {settings.max_message_chars} characters.",
            )
        )
    if len(request.files) > settings.max_files_per_request:
        findings.append(
            GuardrailFinding(
                category="excessive_resource",
                severity="medium",
                detail=f"More than {settings.max_files_per_request} files attached.",
            )
        )
    for match in scan(request.message[: settings.max_message_chars * 2]):
        findings.append(GuardrailFinding(category=match.category, severity=match.severity, detail=match.detail))

    if not findings:
        return GuardrailVerdict(stage="input", action="allow")
    enforce = settings.guardrail_mode.lower() != "monitor"
    blocking = [finding for finding in findings if finding.severity in {"high", "medium"}]
    if enforce and blocking:
        return GuardrailVerdict(
            stage="input", action="block", findings=findings, message=refusal_message(blocking)
        )
    return GuardrailVerdict(stage="input", action="allow", findings=findings)
