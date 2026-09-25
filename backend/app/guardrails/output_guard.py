"""Output guardrail: the last stage before a response leaves the platform.

* Redacts configured secret values and credential-shaped strings.
* Redacts high-risk PII (cards, SSNs; e-mail/phone in strict mode).
* Blocks responses that leak the system prompt (canary token or long verbatim
  fragments of registered system prompts).
"""

from __future__ import annotations

import re

from app.agents.prompts import CANARY, registered_prompt_texts
from app.config import get_settings
from app.guardrails.detectors import normalize, redact_pii, redact_secrets
from app.models import GuardrailFinding, GuardrailVerdict

SHINGLE_WORDS = 10
LEAK_WITHHELD = (
    "The generated response was withheld because it appeared to contain internal "
    "system instructions. Please rephrase your request."
)


def _shingles(text: str, size: int = SHINGLE_WORDS) -> set[str]:
    words = re.findall(r"[a-z0-9']+", normalize(text))
    return {" ".join(words[index : index + size]) for index in range(0, max(0, len(words) - size + 1))}


def _leaks_system_prompt(text: str) -> bool:
    if CANARY in text:
        return True
    output_shingles = _shingles(text)
    if not output_shingles:
        return False
    for prompt in registered_prompt_texts():
        if output_shingles & _shingles(prompt):
            return True
    return False


def inspect_output(text: str) -> tuple[str, GuardrailVerdict]:
    settings = get_settings()
    findings: list[GuardrailFinding] = []

    if _leaks_system_prompt(text):
        findings.append(
            GuardrailFinding(
                category="system_prompt_leak",
                severity="high",
                detail="Response contained internal instruction text.",
            )
        )
        return LEAK_WITHHELD, GuardrailVerdict(
            stage="output", action="block", findings=findings, message=LEAK_WITHHELD
        )

    redacted, secret_kinds = redact_secrets(text, settings.secret_values())
    if secret_kinds:
        findings.append(
            GuardrailFinding(
                category="secret_leak",
                severity="high",
                detail="Redacted: " + ", ".join(sorted(set(secret_kinds))),
            )
        )
    redacted, pii_kinds = redact_pii(redacted, strict=settings.pii_redaction.lower() == "strict")
    if pii_kinds:
        findings.append(
            GuardrailFinding(category="pii_leak", severity="medium", detail="Redacted: " + ", ".join(pii_kinds))
        )
    action = "redact" if findings else "allow"
    return redacted, GuardrailVerdict(stage="output", action=action, findings=findings)
