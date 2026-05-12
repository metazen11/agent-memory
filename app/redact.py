"""Secret and PII redaction for the observation pipeline."""

import re

from app.config import settings

SECRET_PATTERNS = [
    (re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]{80,}", re.I), "[REDACTED:anthropic_key]"),
    (re.compile(r"sk-[A-Za-z0-9]{48,}"), "[REDACTED:openai_key]"),
    (re.compile(r"hf_[A-Za-z0-9]{30,}"), "[REDACTED:hf_token]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED:github_token]"),
    (re.compile(r"gho_[A-Za-z0-9]{36,}"), "[REDACTED:github_oauth]"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "[REDACTED:gitlab_token]"),
    (re.compile(r"xox[bpors]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
    (re.compile(r"mem_[A-Za-z0-9_-]{32,}"), "[REDACTED:agent_memory_token]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.I), "[REDACTED:bearer_token]"),
    (
        re.compile(
            r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
            r"-----END\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
        "[REDACTED:private_key]",
    ),
    (re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*\S+", re.I), "[REDACTED:password]"),
    (re.compile(r"(?:postgresql|mysql|mongodb)://[^\s]+@[^\s]+", re.I), "[REDACTED:connection_string]"),
]

PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[REDACTED:email]"),
    (re.compile(r"\b\d{3}[-.]?\d{2}[-.]?\d{4}\b"), "[REDACTED:ssn]"),
]


def redact_text(text: str | None) -> str | None:
    """Redact secrets and optionally PII from text."""
    if not text:
        return text
    if settings.redact_secrets:
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
    if settings.redact_pii:
        for pattern, replacement in PII_PATTERNS:
            text = pattern.sub(replacement, text)
    return text
