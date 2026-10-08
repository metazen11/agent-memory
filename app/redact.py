"""Secret and PII redaction for the observation pipeline."""

from app.config import settings
from app.redact_patterns import (
    SECRET_PATTERNS,
    PATH_PATTERNS,
    PII_PATTERNS,
)

__all__ = [
    "SECRET_PATTERNS",
    "PATH_PATTERNS",
    "PII_PATTERNS",
    "redact_text",
    "redact_json",
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
    if getattr(settings, "redact_paths", True):
        for pattern, replacement in PATH_PATTERNS:
            text = pattern.sub(replacement, text)
    return text


def redact_json(obj):
    """Recursively redact every string leaf of a JSON-shaped value.

    Walks dicts and lists; replaces each string leaf with the result of
    ``redact_text``. Non-string leaves (numbers, bools, None) pass through
    unchanged. Returns a new structure; the input is not mutated.

    Used by the backfill writer and any other path that ingests nested
    JSON from external sources (tool_input, tool_response, etc.). The
    plain ``redact_text`` is only safe for top-level strings; secrets
    nested inside JSON (e.g. ``{"headers": {"Authorization": "Bearer ..."}}``)
    would slip past it unless every leaf is walked.
    """
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
