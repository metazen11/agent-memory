"""API token authentication for agent-memory."""

import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from app.config import settings
from app.db import get_pool

logger = logging.getLogger(__name__)


def hash_token(raw_token: str) -> str:
    """SHA-256 hash a raw token for storage."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def generate_token() -> str:
    """Generate a cryptographically secure API token."""
    return f"mem_{secrets.token_urlsafe(32)}"


# Header-derived agent names are attacker-controlled, so they are never used
# raw. They are lowercased, stripped to a conservative charset, and truncated
# before being used as a dict key anywhere (notably the rate limiter's
# never-evicted `_buckets` map — see app/middleware.py). Without the cap, a
# caller could mint unbounded distinct keys and grow that dict without limit.
MAX_AGENT_NAME_LEN = 64
_AGENT_NAME_SAFE_RE = re.compile(r"[^a-z0-9._-]+")


def sanitize_agent_name(raw: str | None) -> str:
    """Normalize a caller-supplied agent name into a safe, bounded identifier.

    Returns "" when nothing usable remains. Callers must treat "" as
    "no agent name supplied" and fall back to another identity source —
    never as "allowed".
    """
    if not raw:
        return ""
    cleaned = _AGENT_NAME_SAFE_RE.sub("-", raw.strip().lower()).strip("-")
    return cleaned[:MAX_AGENT_NAME_LEN]


def trusted_agent_list() -> list[str]:
    """The configured trusted-agent names, lowercased and de-blanked."""
    if not settings.trusted_agents:
        return []
    return [a.strip().lower() for a in settings.trusted_agents.split(",") if a.strip()]


def resolve_agent_identity(request: Request) -> str:
    """Resolve a caller's agent identity from request headers.

    ``X-Agent-Name`` is resolved *completely* before ``User-Agent`` is looked
    at. That ordering is load bearing: ``python-httpx`` is in the default
    trusted list, so a single "scan both headers against the trusted list"
    pass would collapse every httpx-based caller — the pytest suite included —
    into one shared "python-httpx" identity, recreating the exact shared-bucket
    failure issue #63 exists to fix. The explicit header always wins.

    Within each header, a match against ``settings.trusted_agents`` returns the
    *configured* trusted name rather than the raw value, keeping the identity
    space closed over a known-small set so a hostile header cannot mint
    arbitrary identities. A non-matching but non-empty header still yields a
    sanitized identity, so unknown-but-self-identifying callers get their own
    bucket instead of silently sharing one. Returns "" when no usable name is
    present.

    Shared by the auth trusted-caller check and the rate limiter, which must
    agree on who the caller is.
    """
    trusted = [t for t in trusted_agent_list() if t != "*"]

    for raw in (request.headers.get("X-Agent-Name", ""), request.headers.get("User-Agent", "")):
        if not raw:
            continue
        lowered = raw.lower()
        for t in trusted:
            if t in lowered:
                return t
        identity = sanitize_agent_name(raw)
        if identity:
            return identity

    return ""


def _is_trusted_caller(request: Request) -> bool:
    """Check if the request comes from a trusted agent (localhost + User-Agent match).

    Trusted agents bypass token auth so existing integrations (Anvil middleware,
    MCP server, hooks) keep working without tokens during migration.
    """
    trusted = trusted_agent_list()
    if not trusted:
        return False

    # Only trust localhost callers
    client_ip = request.client.host if request.client else None
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return False

    # Match User-Agent or X-Agent-Name header against trusted list
    identity = resolve_agent_identity(request)
    if identity and identity in trusted:
        logger.debug("Trusted agent bypass: %s", identity)
        return True

    # Also trust any localhost caller if "*" is in the trusted list
    if "*" in trusted:
        return True

    return False


async def validate_token(request: Request) -> dict | None:
    """Validate Bearer token from Authorization header.

    Returns token record dict if valid, None if auth is disabled or caller is trusted.
    Raises HTTPException if token is invalid/missing.
    """
    if not settings.require_auth:
        return None

    # Trusted agents bypass auth (localhost + matching agent name)
    if _is_trusted_caller(request):
        return None

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header. "
            "Generate a token with: python -m app.cli create-token --agent <name>",
        )

    raw_token = auth_header[7:]
    token_hash = hash_token(raw_token)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, agent_name, scopes, expires_at FROM mem_api_tokens "
            "WHERE token_hash = $1 AND is_active = true",
            token_hash,
        )
        if not row:
            raise HTTPException(status_code=401, detail="Invalid API token")

        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Token expired")

        # Update last_used_at (fire-and-forget)
        await conn.execute(
            "UPDATE mem_api_tokens SET last_used_at = now() WHERE id = $1",
            row["id"],
        )

        return dict(row)


async def require_scope(token_record: dict | None, scope: str) -> None:
    """Check that a validated token has the required scope."""
    if token_record is None:  # auth disabled
        return
    if scope not in token_record["scopes"]:
        raise HTTPException(
            status_code=403,
            detail=f"Token for '{token_record['agent_name']}' lacks scope '{scope}'",
        )
