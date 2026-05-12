"""API token authentication for agent-memory."""

import hashlib
import logging
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


async def validate_token(request: Request) -> dict | None:
    """Validate Bearer token from Authorization header.

    Returns token record dict if valid, None if auth is disabled.
    Raises HTTPException if token is invalid/missing.
    """
    if not settings.require_auth:
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
