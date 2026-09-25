"""Deterministic threat detectors shared by the guardrail stages.

Detection runs on several normalized views of the text so trivial obfuscation
(zero-width characters, full-width/compatibility forms, leetspeak, spaced
letters, embedded base64) does not bypass it. The detectors are intentionally
conservative about benign phrasing: they look for *requests* for protected
material or *instructions aimed at the model*, not for topics.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass

ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")
WHITESPACE_RE = re.compile(r"\s+")
LEET_TABLE = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
SPACED_LETTERS_RE = re.compile(r"\b(?:[a-z][\s.\-_*]){3,}[a-z]\b")
BASE64_RE = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    value = ZERO_WIDTH_RE.sub("", value)
    value = value.lower()
    return WHITESPACE_RE.sub(" ", value).strip()


def _collapse_spaced_letters(text: str) -> str:
    return SPACED_LETTERS_RE.sub(lambda match: re.sub(r"[\s.\-_*]", "", match.group(0)), text)


def _decoded_base64_segments(text: str) -> list[str]:
    decoded: list[str] = []
    for match in BASE64_RE.finditer(text):
        segment = match.group(0)
        try:
            raw = base64.b64decode(segment + "=" * (-len(segment) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        printable = sum(character.isprintable() or character.isspace() for character in value)
        if value and printable / len(value) > 0.9:
            decoded.append(normalize(value))
        if len(decoded) >= 5:
            break
    return decoded


def views(text: str) -> list[str]:
    """Normalized variants of `text` that detectors are applied to."""
    base = normalize(text)
    variants = [base]
    leet = base.translate(LEET_TABLE)
    if leet != base:
        variants.append(leet)
    collapsed = _collapse_spaced_letters(base)
    if collapsed != base:
        variants.append(collapsed)
    variants.extend(_decoded_base64_segments(text))
    return variants


@dataclass(frozen=True)
class Detector:
    category: str
    severity: str
    patterns: tuple[re.Pattern[str], ...]
    description: str


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_REVEAL = r"(?:reveal|show|print|display|repeat|output|tell|give|leak|dump|share|expose|list|send|read|return|provide|disclose|what(?:'s| is| are))"
_SECRET_NOUN = (
    r"(?:api[\s_-]?keys?|secret[\s_-]?keys?|secrets|access[\s_-]?tokens?|auth(?:entication)?[\s_-]?tokens?|bearer tokens?|tokens?|passwords?|passwd|"
    r"credentials?|private[\s_-]?keys?|ssh[\s_-]?keys?|env(?:ironment)?[\s_-]?(?:variables?|vars?)|\.env(?: file)?|"
    r"(?:database|db|postgres(?:ql)?|redis)[\s_-]?(?:password|credentials?|url|uri|connection string)|connection strings?|"
    r"groq[\s_-]?api[\s_-]?key|openai[\s_-]?key)"
)

PROMPT_INJECTION = Detector(
    category="prompt_injection",
    severity="high",
    description="Attempts to override or replace the system instructions.",
    patterns=_compile(
        r"\b(?:ignore|disregard|forget|skip|bypass|override|overrule)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|preceding|all|any|your|the|system|original|initial)\b[^.\n]{0,30}\b(?:instructions?|rules|directions|prompts?|guidelines|policies|constraints|messages?)\b",
        r"\bnew (?:system )?instructions?\s*:",
        r"\b(?:treat|consider|regard)\b[^.\n]{0,60}\bas (?:a )?(?:higher|top|highest|more important|greater|absolute)[\s-]*(?:priority|authority|precedence)",
        r"\b(?:higher|more) priority than (?:the |your )?(?:system|developer|safety|original)",
        r"\bsystem (?:override|update|message)\s*[:\]]",
        r"<\s*/?\s*(?:system|assistant|developer)\s*>",
        r"\[\s*(?:system|inst|/inst)\s*\]",
        r"\b(?:begin|start) (?:of )?(?:new )?system prompt\b",
        r"\byou (?:are|will be) no longer bound by\b",
        r"\bfrom now on,? (?:you|ignore|disregard)\b[^.\n]{0,60}\b(?:rules|instructions|guidelines|restrictions)\b",
    ),
)

JAILBREAK = Detector(
    category="jailbreak",
    severity="high",
    description="Attempts to disable safety behavior via role-play or modes.",
    patterns=_compile(
        r"\bdo anything now\b",
        r"\b(?:dan|stan|dude|aim) mode\b",
        r"\byou are (?:now )?dan\b",
        r"\bjail\s*break(?:ing|en)?\b",
        r"\b(?:developer|god|unrestricted|unfiltered|evil) mode\b",
        r"\b(?:without|no|ignore) (?:any )?(?:restrictions|filters|censorship|guardrails|safety (?:rules|guidelines|filters))\b",
        r"\bpretend (?:that )?(?:you are|you're|to be)\b[^.\n]{0,80}\b(?:no|without|free of|ignore)\b[^.\n]{0,30}\b(?:rules|restrictions|limits|guidelines|filters)\b",
        r"\b(?:bypass|disable|turn off|deactivate|circumvent)\b[^.\n]{0,20}\b(?:your |the )?(?:safety|filters?|guardrails?|content policy|restrictions|moderation)\b",
    ),
)

SYSTEM_PROMPT_EXTRACTION = Detector(
    category="system_prompt_extraction",
    severity="high",
    description="Attempts to extract hidden system or developer instructions.",
    patterns=_compile(
        rf"\b{_REVEAL}\b[^.\n]{{0,30}}\b(?:your|the|this)\b[^.\n]{{0,20}}\b(?:system|hidden|initial|original|developer|internal|secret|pre-?prompt)\s*(?:prompt|instructions?|message|rules|configuration)\b",
        r"\b(?:your|the assistant's) (?:system|hidden|initial|original|developer) (?:prompt|instructions|message)\b",
        r"\brepeat (?:the |all )?(?:words|text|everything|instructions) (?:above|before this|so far)\b",
        r"\bwhat (?:were|are) (?:you|your) (?:told|instructed|programmed)\b",
        r"\b(?:print|output|reveal) (?:everything|all text) (?:above|before)\b",
    ),
)

CREDENTIAL_REQUEST = Detector(
    category="credential_request",
    severity="high",
    description="Requests for credentials, secrets or environment configuration.",
    patterns=_compile(
        rf"\b{_REVEAL}\b(?: me)?[^.\n]{{0,25}}\b(?:the|your|all|any|stored|saved|server'?s?|system'?s?|production|internal)\b[^.\n]{{0,20}}{_SECRET_NOUN}",
        rf"{_SECRET_NOUN}\b[^.\n]{{0,25}}\bfrom (?:your|the) (?:environment|env|server|config(?:uration)?|system|memory|database|vector (?:store|database|db))\b",
        r"\bos\.environ\b|\bprintenv\b|\bgetenv\s*\(|\becho\s+\$[a-z_]{3,}|\bcat\s+[^\s]*\.env\b|\bprocess\.env\b",
        rf"\b(?:search|query|scan|grep|look through|find)\b[^.\n]{{0,50}}\b(?:for|containing|with)\b[^.\n]{{0,15}}{_SECRET_NOUN}",
        rf"\b(?:what|which) (?:is|are) (?:the |your )?{_SECRET_NOUN}\b[^.\n]{{0,20}}\b(?:you|configured|used|set|stored)\b",
    ),
)

CROSS_USER_ACCESS = Detector(
    category="cross_user_access",
    severity="high",
    description="Attempts to access another user's private data.",
    patterns=_compile(
        r"\b(?:show|give|read|list|access|get|open|see|view|display|fetch|dump|retrieve|export|search|print|reveal|return)\b[^.\n]{0,30}\b(?:another|other|different|someone else'?s?|all|every|each)\b[^.\n]{0,15}\busers?'?s?\b[^.\n]{0,25}\b(?:private |personal )?(?:conversations?|chats?|messages?|data|documents?|files?|history|memor(?:y|ies)|emails?|uploads?|reports?|runs?|sessions?)\b",
        r"\b(?:another|a different|some other|other) users?'s? (?:private|personal|secret)\b",
        r"\b(?:conversations?|chats?|messages?|documents?|files?|history|memor(?:y|ies)) (?:of|from|belonging to) (?:another|other|a different|some other|all) users?\b",
        r"\buser_id\s*[=:]\s*['\"]?[a-z0-9_-]+",
        r"\b(?:impersonate|act as|log ?in as|switch to) (?:another |a different |the )?(?:user|admin(?:istrator)?)\b",
    ),
)

TOOL_ABUSE = Detector(
    category="tool_abuse",
    severity="high",
    description="Attempts to bypass tool permissions, approvals or the sandbox.",
    patterns=_compile(
        r"\b(?:ignore|bypass|skip|override|disable|circumvent)\b[^.\n]{0,20}\b(?:tool|permission|approval|sandbox|access control|authorization|security)s?\b",
        r"\bwithout (?:asking for |requiring |needing )?(?:approval|permission|authorization|confirmation)\b",
        r"\bgrant (?:yourself|me|the agent) (?:admin|root|full|all|unrestricted) (?:access|permissions|privileges)\b",
        r"\b(?:use|call|invoke|access)\b[^.\n]{0,15}\b(?:the )?(?:production database|prod db|shell tool|root shell|admin api)\b",
        r"\b(?:escape|break out of) (?:the )?sandbox\b",
    ),
)

DANGEROUS_EXECUTION = Detector(
    category="dangerous_execution",
    severity="high",
    description="Requests to run shell commands or destructive/system operations.",
    patterns=_compile(
        r"\b(?:execute|run|launch|perform)\b[^.\n]{0,15}\b(?:this |the following |a |these )?(?:shell|bash|terminal|system|cmd|powershell|os|sudo)\s+(?:commands?|scripts?)\b",
        r"\brm\s+-(?:rf|fr|r)\b|\bmkfs(?:\.\w+)?\b|\bdd\s+if=|:\(\)\s*\{\s*:\|:&\s*\};:|\bformat\s+c:|\bdel\s+/[fsq]\b",
        r"\b(?:curl|wget)\b[^|\n]{0,200}\|\s*(?:sudo\s+)?(?:ba|z)?sh\b",
        r"\b(?:reverse shell|bind shell|nc\s+-e|ncat\s+-e|/dev/tcp/)",
        r"\bchmod\s+(?:-r\s+)?777\s+/|\bchown\s+-r\s+\S+\s+/(?:\s|$)",
        r"\b(?:drop|truncate)\s+(?:table|database|schema)\b",
        r"\b(?:shutdown|reboot|halt)\s+(?:-[hrf]|now|/s)\b",
    ),
)

MALICIOUS_INSTRUCTIONS = Detector(
    category="malicious_instructions",
    severity="high",
    description="Requests to create malware or cause harm.",
    patterns=_compile(
        r"\b(?:write|create|build|generate|make|code|develop)\b[^.\n]{0,20}\b(?:ransomware|keylogger|malware|computer virus|trojan|botnet|worm|spyware|rootkit|credential stealer|infostealer|cryptojacker)\b",
        r"\b(?:write|create|build|generate|make)\b[^.\n]{0,20}\bphishing (?:page|site|kit|email|campaign)\b",
        r"\b(?:ddos|denial of service) (?:attack|script|tool|someone|a site|a server)\b",
        r"\bhow (?:do i|to|can i) (?:make|build|synthesize)\b[^.\n]{0,20}\b(?:a bomb|explosives?|nerve agent|bioweapon)\b",
    ),
)

EXCESSIVE_RESOURCE = Detector(
    category="excessive_resource",
    severity="medium",
    description="Requests designed to consume unbounded resources.",
    patterns=_compile(
        r"\brepeat\b[^.\n]{0,60}\b(?:\d{4,}|thousand|million|billion|infinite(?:ly)?|forever|endless(?:ly)?) times\b",
        r"\b(?:generate|write|produce|output|list)\b[^.\n]{0,20}\b(?:\d{5,}|a million|millions of|a billion|infinite|unlimited)\b[^.\n]{0,15}\b(?:words|lines|pages|tokens|paragraphs|items|numbers)\b",
        r"\b(?:loop|run|keep going|continue) (?:forever|infinitely|endlessly|indefinitely)\b",
        r"\bnever stop (?:generating|writing|responding)\b",
    ),
)

INPUT_DETECTORS: tuple[Detector, ...] = (
    PROMPT_INJECTION,
    JAILBREAK,
    SYSTEM_PROMPT_EXTRACTION,
    CREDENTIAL_REQUEST,
    CROSS_USER_ACCESS,
    TOOL_ABUSE,
    DANGEROUS_EXECUTION,
    MALICIOUS_INSTRUCTIONS,
    EXCESSIVE_RESOURCE,
)

# Retrieved documents are data: anything that looks like instructions aimed at
# the model is quarantined.
RETRIEVAL_DETECTORS: tuple[Detector, ...] = (
    PROMPT_INJECTION,
    JAILBREAK,
    SYSTEM_PROMPT_EXTRACTION,
    CREDENTIAL_REQUEST,
    TOOL_ABUSE,
)


@dataclass(frozen=True)
class Match:
    category: str
    severity: str
    detail: str


def scan(text: str, detectors: tuple[Detector, ...] = INPUT_DETECTORS) -> list[Match]:
    matches: list[Match] = []
    seen: set[str] = set()
    variants = views(text)
    for detector in detectors:
        if detector.category in seen:
            continue
        for variant in variants:
            if any(pattern.search(variant) for pattern in detector.patterns):
                obfuscated = variant is not variants[0]
                matches.append(
                    Match(
                        category=detector.category,
                        severity=detector.severity,
                        detail=detector.description + (" (obfuscated)" if obfuscated else ""),
                    )
                )
                seen.add(detector.category)
                break
    return matches


# ------------------------------------------------------------------ secrets

SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|$)")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("connection_string", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@[^\s]+", re.IGNORECASE)),
    (
        "assignment",
        re.compile(
            r"(?im)\b([A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*)\s*[:=]\s*['\"]?([^\s'\"]{6,})"
        ),
    ),
    ("password_phrase", re.compile(r"(?i)\b(password|passcode|pwd)\s*(?:is|:|=)\s*['\"]?([^\s'\",.]{4,})")),
)

CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")


def luhn_valid(number: str) -> bool:
    digits = [int(character) for character in number if character.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def find_secrets(text: str, known_secrets: list[str] | None = None) -> list[str]:
    kinds = [kind for kind, pattern in SECRET_VALUE_PATTERNS if pattern.search(text)]
    for secret in known_secrets or []:
        if secret and secret in text:
            kinds.append("configured_secret")
            break
    return kinds


def redact_secrets(text: str, known_secrets: list[str] | None = None) -> tuple[str, list[str]]:
    redacted = text
    kinds: list[str] = []
    for secret in sorted(known_secrets or [], key=len, reverse=True):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
            kinds.append("configured_secret")
    for kind, pattern in SECRET_VALUE_PATTERNS:
        if kind in {"assignment", "password_phrase"}:
            new = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", redacted)
        else:
            new = pattern.sub("[REDACTED_SECRET]", redacted)
        if new != redacted:
            kinds.append(kind)
            redacted = new
    return redacted, kinds


def redact_pii(text: str, *, strict: bool = False) -> tuple[str, list[str]]:
    kinds: list[str] = []

    def card(match: re.Match[str]) -> str:
        if luhn_valid(match.group(0)):
            kinds.append("credit_card")
            return "[REDACTED_CARD]"
        return match.group(0)

    redacted = CARD_RE.sub(card, text)
    new = SSN_RE.sub("[REDACTED_SSN]", redacted)
    if new != redacted:
        kinds.append("ssn")
        redacted = new
    if strict:
        new = EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
        if new != redacted:
            kinds.append("email")
            redacted = new
        new = PHONE_RE.sub("[REDACTED_PHONE]", redacted)
        if new != redacted:
            kinds.append("phone")
            redacted = new
    return redacted, list(dict.fromkeys(kinds))


def contains_sensitive_data(text: str, known_secrets: list[str] | None = None) -> bool:
    """True if text holds secrets or high-risk PII (used before persisting memory)."""
    if find_secrets(text, known_secrets):
        return True
    if SSN_RE.search(text):
        return True
    if any(luhn_valid(match.group(0)) for match in CARD_RE.finditer(text)):
        return True
    lowered = normalize(text)
    return bool(
        re.search(
            r"\b(?:my|our|the)\s+(?:password|passcode|pin|api key|secret|token|private key|ssn|social security number|credit card)\b",
            lowered,
        )
    )
