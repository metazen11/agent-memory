"""API routes for user prompt search and retrieval.

Writes (``POST /api/prompts``) capture user prompts on every UserPromptSubmit
hook event. Writes go through ``ensure_project`` so prompts land under the
canonical git-root project (matching the tool-call writer in
``observations.py``). ``content_hash`` + ``(session_id, turn_index)`` uniqueness
makes the write path idempotent — re-firing the hook for the same prompt
returns the existing row.
"""

import hashlib
import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.db import get_pool
from app.path_normalize import normalize_text
from app.project import ensure_project
from app.redact import redact_text

logger = logging.getLogger(__name__)
router = APIRouter()


# ── SQL ───────────────────────────────────────────────

_ENSURE_SESSION_SQL = """
INSERT INTO mem_sessions (session_id, project_id, agent_type)
VALUES ($1, $2, $3)
ON CONFLICT (session_id) DO UPDATE SET
    project_id = COALESCE(mem_sessions.project_id, EXCLUDED.project_id)
RETURNING id
"""

_NEXT_PROMPT_NUMBER_SQL = (
    "SELECT COALESCE(MAX(prompt_number), 0) + 1 "
    "FROM mem_user_prompts WHERE session_id = $1"
)

_FIND_PROMPT_BY_HASH_SQL = (
    "SELECT id, prompt_number FROM mem_user_prompts "
    "WHERE session_id = $1 AND content_hash = $2"
)

_INSERT_PROMPT_SQL = """
INSERT INTO mem_user_prompts
    (session_id, project_id, prompt_number, prompt_text, agent_name,
     turn_index, content_hash, retention_class)
VALUES ($1, $2, $3, $4, $5, $3, $6, 'live')
RETURNING id, prompt_number
"""


class PromptSearchRequest(BaseModel):
    query: str
    project: Optional[str] = None
    agent: Optional[str] = None
    limit: int = 20


class PromptOut(BaseModel):
    id: int
    session_id: int
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    prompt_number: int
    prompt_text: str
    agent_name: Optional[str] = None
    created_at: str
    score: Optional[float] = None


class PromptCreate(BaseModel):
    """Payload from the UserPromptSubmit hook."""
    session_id: str
    prompt: str
    cwd: Optional[str] = None
    agent_name: Optional[str] = "claude-code"


def _content_hash(text: str) -> str:
    """Stable hash for idempotency. SHA-256 of the prompt bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@router.get("/api/prompts")
async def list_prompts(
    request: Request,
    project: Optional[str] = Query(None),
    session_id: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """List prompts with optional filters."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        args = []
        idx = 1

        if project:
            conditions.append(f"p.full_path ILIKE ${idx}")
            args.append(f"%{project}%")
            idx += 1
        if session_id:
            conditions.append(f"up.session_id = ${idx}")
            args.append(session_id)
            idx += 1
        if agent:
            conditions.append(f"up.agent_name = ${idx}")
            args.append(agent)
            idx += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = await conn.fetch(
            f"""
            SELECT up.id, up.session_id, up.project_id, p.full_path as project_name,
                   up.prompt_number, up.prompt_text, up.agent_name, up.created_at
            FROM mem_user_prompts up
            LEFT JOIN mem_projects p ON up.project_id = p.id
            {where}
            ORDER BY up.created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *args,
            limit,
            offset,
        )

    return {"prompts": [dict(r) for r in rows], "total": len(rows)}


@router.post("/api/prompts")
async def create_prompt(request: Request, body: PromptCreate):
    """Record a user prompt from the UserPromptSubmit hook.

    Idempotent on ``(session_id, content_hash)`` — re-firing the hook for
    the same prompt returns the existing row instead of inserting a
    duplicate. Each unique prompt within a session gets the next
    ``prompt_number`` (1, 2, 3, …); ``turn_index`` mirrors this for the
    v2 schema's turn-ordering scheme.

    The prompt text is redacted of known secret shapes before storage.
    """
    pool = await get_pool()
    # Path normalization (migration 014): rewrite stale /Dropbox/_CODING/ to
    # /_CODING/ at the write boundary on both the prompt body AND the cwd,
    # so re-fired hooks from a stale shell never reintroduce the bias path.
    # Normalize BEFORE hashing so identical normalized prompts dedupe via
    # the content_hash idempotency key.
    redacted = normalize_text(redact_text(body.prompt)) or ""
    content_hash = _content_hash(redacted)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # mem_sessions.project_id is NOT NULL, so fall back to the
            # 'unknown' project the way observations.py does for queue
            # writes without a cwd.
            project_path = normalize_text(body.cwd) or "unknown"
            project_id = await ensure_project(conn, project_path)

            session_row = await conn.fetchrow(
                _ENSURE_SESSION_SQL,
                body.session_id, project_id, body.agent_name or "claude-code",
            )
            session_db_id = session_row["id"]

            # Idempotency check.
            existing = await conn.fetchrow(
                _FIND_PROMPT_BY_HASH_SQL,
                session_db_id, content_hash,
            )
            if existing:
                return {
                    "id": existing["id"],
                    "prompt_number": existing["prompt_number"],
                    "status": "exists",
                }

            prompt_number = await conn.fetchval(
                _NEXT_PROMPT_NUMBER_SQL, session_db_id
            )

            inserted = await conn.fetchrow(
                _INSERT_PROMPT_SQL,
                session_db_id, project_id, prompt_number,
                redacted, body.agent_name, content_hash,
            )

    return {
        "id": inserted["id"],
        "prompt_number": inserted["prompt_number"],
        "status": "created",
    }


@router.post("/api/prompts/search")
async def search_prompts(request: Request, body: PromptSearchRequest):
    """Hybrid search over user prompts (FTS + optional vector)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = ["up.tsv @@ plainto_tsquery('english', $1)"]
        args = [body.query]
        idx = 2

        if body.project:
            conditions.append(f"p.full_path ILIKE ${idx}")
            args.append(f"%{body.project}%")
            idx += 1
        if body.agent:
            conditions.append(f"up.agent_name = ${idx}")
            args.append(body.agent)
            idx += 1

        where = " AND ".join(conditions)

        rows = await conn.fetch(
            f"""
            SELECT up.id, up.session_id, up.project_id, p.full_path as project_name,
                   up.prompt_number, up.prompt_text, up.agent_name, up.created_at,
                   ts_rank(up.tsv, plainto_tsquery('english', $1)) as score
            FROM mem_user_prompts up
            LEFT JOIN mem_projects p ON up.project_id = p.id
            WHERE {where}
            ORDER BY score DESC
            LIMIT ${idx}
            """,
            *args,
            body.limit,
        )

    return {"prompts": [dict(r) for r in rows], "query": body.query, "total": len(rows)}


@router.get("/api/prompts/{prompt_id}")
async def get_prompt(request: Request, prompt_id: int):
    """Get a single prompt by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT up.*, p.full_path as project_name
            FROM mem_user_prompts up
            LEFT JOIN mem_projects p ON up.project_id = p.id
            WHERE up.id = $1
            """,
            prompt_id,
        )
    if not row:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Prompt not found")
    return dict(row)
