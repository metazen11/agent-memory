"""API routes for user prompt search and retrieval."""

import logging
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.db import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


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
