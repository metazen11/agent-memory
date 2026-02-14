import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.db import get_pool
from app.models import SessionCreate, SessionUpdate, SessionOut

logger = logging.getLogger(__name__)

router = APIRouter()


async def _ensure_project(conn, name: str, full_path: str | None = None) -> int:
    """Get or create a project by name."""
    row = await conn.fetchrow("SELECT id FROM mem_projects WHERE name = $1", name)
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO mem_projects (name, full_path) VALUES ($1, $2) RETURNING id",
        name, full_path,
    )
    return row["id"]


@router.post("/api/sessions", response_model=SessionOut)
async def create_session(req: SessionCreate):
    """Start a new session."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check if session already exists
        existing = await conn.fetchrow(
            "SELECT id FROM mem_sessions WHERE session_id = $1", req.session_id
        )
        if existing:
            raise HTTPException(status_code=409, detail="Session already exists")

        project_id = await _ensure_project(conn, req.project, req.project_path)

        row = await conn.fetchrow("""
            INSERT INTO mem_sessions (session_id, project_id, agent_type)
            VALUES ($1, $2, $3)
            RETURNING id, session_id, project_id, agent_type, status,
                      started_at, completed_at, summary, prompt_count
        """, req.session_id, project_id, req.agent_type)

        return SessionOut(
            id=row["id"],
            session_id=row["session_id"],
            project_id=row["project_id"],
            project_name=req.project,
            agent_type=row["agent_type"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            summary=row["summary"],
            prompt_count=row["prompt_count"],
        )


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: SessionUpdate):
    """Update session status (e.g. mark completed)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM mem_sessions WHERE session_id = $1", session_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")

        updates = []
        params = []
        param_idx = 1

        if req.status:
            updates.append(f"status = ${param_idx}")
            params.append(req.status)
            param_idx += 1
            if req.status == "completed":
                updates.append(f"completed_at = now()")

        if req.summary:
            updates.append(f"summary = ${param_idx}")
            params.append(req.summary)
            param_idx += 1

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        params.append(session_id)
        await conn.execute(
            f"UPDATE mem_sessions SET {', '.join(updates)} WHERE session_id = ${param_idx}",
            *params,
        )

        return {"status": "updated"}


@router.get("/api/sessions")
async def list_sessions(
    project: str | None = None,
    status: str | None = None,
    limit: int = Query(default=20, le=100),
):
    """List sessions with optional filters."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        param_idx = 1

        if project:
            conditions.append(f"p.name = ${param_idx}")
            params.append(project)
            param_idx += 1

        if status:
            conditions.append(f"s.status = ${param_idx}")
            params.append(status)
            param_idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        rows = await conn.fetch(f"""
            SELECT s.id, s.session_id, s.project_id, p.name as project_name,
                   s.agent_type, s.status, s.started_at, s.completed_at,
                   s.summary, s.prompt_count
            FROM mem_sessions s
            JOIN mem_projects p ON p.id = s.project_id
            {where}
            ORDER BY s.started_at DESC
            LIMIT ${param_idx}
        """, *params)

        return [
            SessionOut(
                id=r["id"],
                session_id=r["session_id"],
                project_id=r["project_id"],
                project_name=r["project_name"],
                agent_type=r["agent_type"],
                status=r["status"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
                summary=r["summary"],
                prompt_count=r["prompt_count"],
            )
            for r in rows
        ]
