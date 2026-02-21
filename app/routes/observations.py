import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.db import get_pool
from app.models import QueueItem, ObservationCreate, ObservationOut, SearchRequest, SearchResult
from app.embeddings import embed_text

logger = logging.getLogger(__name__)

router = APIRouter()


async def _ensure_project(conn, name: str, full_path: str | None = None) -> int:
    """Get or create a project by name. Returns project id."""
    row = await conn.fetchrow(
        "SELECT id FROM mem_projects WHERE name = $1", name
    )
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO mem_projects (name, full_path) VALUES ($1, $2) RETURNING id",
        name, full_path,
    )
    return row["id"]


async def _ensure_session(conn, session_id: str, project_id: int, agent_type: str = "claude-code") -> int:
    """Get or create a session. Returns session db id."""
    row = await conn.fetchrow(
        "SELECT id FROM mem_sessions WHERE session_id = $1", session_id
    )
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO mem_sessions (session_id, project_id, agent_type) VALUES ($1, $2, $3) RETURNING id",
        session_id, project_id, agent_type,
    )
    return row["id"]


# ── Queue ingest (fire-and-forget from hooks) ────────

@router.post("/api/queue")
async def queue_observation(item: QueueItem):
    """Accept tool call data for async observation processing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Derive project name from cwd
        project_name = Path(item.cwd).name if item.cwd else "unknown"
        project_id = await _ensure_project(conn, project_name, item.cwd)
        session_db_id = await _ensure_session(conn, item.session_id, project_id)

        await conn.execute("""
            INSERT INTO mem_observation_queue
            (session_id, tool_name, tool_input, tool_response_preview, cwd, last_user_message)
            VALUES ($1, $2, $3, $4, $5, $6)
        """,
            session_db_id,
            item.tool_name,
            json.dumps(item.tool_input) if item.tool_input else None,
            item.tool_response_preview[:2000] if item.tool_response_preview else None,
            item.cwd,
            item.last_user_message,
        )

    return {"status": "queued"}


# ── Direct observation creation ───────────────────────

@router.post("/api/observations", response_model=ObservationOut)
async def create_observation(obs: ObservationCreate):
    """Create an observation directly (bypasses queue)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        project_id = await _ensure_project(conn, obs.project)
        session_db_id = await _ensure_session(conn, obs.session_id, project_id)

        raw_text = _build_raw_text(obs)

        # Generate embedding
        embedding_str = None
        embedding_model_id = None
        try:
            embedding = await embed_text(raw_text)
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            model_row = await conn.fetchrow(
                "SELECT id FROM embedding_models WHERE is_default = true LIMIT 1"
            )
            if model_row:
                embedding_model_id = model_row["id"]
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")

        row = await conn.fetchrow("""
            INSERT INTO mem_observations (
                session_id, project_id, title, subtitle, type,
                narrative, facts, concepts, files_read, files_modified,
                raw_text, embedding, embedding_model_id,
                tool_name, prompt_number
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9, $10,
                $11, $12::vector, $13,
                $14, $15
            ) RETURNING id, created_at
        """,
            session_db_id, project_id, obs.title, obs.subtitle, obs.type,
            obs.narrative,
            json.dumps(obs.facts), json.dumps(obs.concepts),
            json.dumps(obs.files_read), json.dumps(obs.files_modified),
            raw_text, embedding_str, embedding_model_id,
            obs.tool_name, obs.prompt_number,
        )

        return ObservationOut(
            id=row["id"],
            session_id=session_db_id,
            project_id=project_id,
            project_name=obs.project,
            title=obs.title,
            subtitle=obs.subtitle,
            type=obs.type,
            narrative=obs.narrative,
            facts=obs.facts,
            concepts=obs.concepts,
            files_read=obs.files_read,
            files_modified=obs.files_modified,
            tool_name=obs.tool_name,
            prompt_number=obs.prompt_number,
            has_embedding=embedding_str is not None,
            created_at=row["created_at"],
        )


# ── List observations ────────────────────────────────

@router.get("/api/observations")
async def list_observations(
    project: str | None = None,
    type: str | None = None,
    limit: int = Query(default=20, le=100),
    offset: int = 0,
):
    """List observations with optional filters."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        param_idx = 1

        if project:
            conditions.append(f"p.name = ${param_idx}")
            params.append(project)
            param_idx += 1

        if type:
            conditions.append(f"o.type = ${param_idx}")
            params.append(type)
            param_idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        params.extend([limit, offset])
        rows = await conn.fetch(f"""
            SELECT o.id, o.session_id, o.project_id, p.name as project_name,
                   o.title, o.subtitle, o.type, o.narrative,
                   o.facts, o.concepts, o.files_read, o.files_modified,
                   o.tool_name, o.prompt_number,
                   o.embedding IS NOT NULL as has_embedding,
                   o.created_at
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            {where}
            ORDER BY o.created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """, *params)

        return [_row_to_obs(r) for r in rows]


# ── Get single observation ────────────────────────────

@router.get("/api/observations/{obs_id}")
async def get_observation(obs_id: int):
    """Get a single observation by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT o.id, o.session_id, o.project_id, p.name as project_name,
                   o.title, o.subtitle, o.type, o.narrative,
                   o.facts, o.concepts, o.files_read, o.files_modified,
                   o.tool_name, o.prompt_number,
                   o.embedding IS NOT NULL as has_embedding,
                   o.created_at
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            WHERE o.id = $1
        """, obs_id)

        if not row:
            raise HTTPException(status_code=404, detail="Observation not found")

        return _row_to_obs(row)


# ── Hybrid search ─────────────────────────────────────

@router.post("/api/observations/search", response_model=SearchResult)
async def search_observations(req: SearchRequest):
    """Hybrid search: vector similarity + full-text search with RRF fusion."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        results = []

        if req.mode in ("vector", "hybrid"):
            # Vector search
            try:
                query_embedding = await embed_text(req.query)
                emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

                # Build query with proper parameter numbering
                # $1 = embedding, $2 = limit
                params: list = [emb_str, req.limit * 2]
                filters = ["o.embedding IS NOT NULL"]
                pidx = 3

                if req.project and not req.cross_project:
                    filters.append(f"p.name = ${pidx}")
                    params.append(req.project)
                    pidx += 1

                if req.type:
                    placeholders = ", ".join(f"${pidx + i}" for i in range(len(req.type)))
                    filters.append(f"o.type IN ({placeholders})")
                    params.extend(req.type)
                    pidx += len(req.type)

                where = " AND ".join(filters)

                vector_rows = await conn.fetch(f"""
                    SELECT o.id, o.session_id, o.project_id, p.name as project_name,
                           o.title, o.subtitle, o.type, o.narrative,
                           o.facts, o.concepts, o.files_read, o.files_modified,
                           o.tool_name, o.prompt_number,
                           true as has_embedding,
                           o.created_at,
                           1 - (o.embedding <=> $1::vector) as similarity
                    FROM mem_observations o
                    JOIN mem_projects p ON p.id = o.project_id
                    WHERE {where}
                    ORDER BY o.embedding <=> $1::vector
                    LIMIT $2
                """, *params)

                for rank, row in enumerate(vector_rows):
                    results.append((row["id"], 1.0 / (rank + 60), row))  # RRF k=60
            except Exception as e:
                logger.warning(f"Vector search failed: {e}")

        if req.mode in ("fts", "hybrid"):
            # Full-text search
            ts_query = " & ".join(req.query.split()[:10])

            # $1 = ts_query, $2 = limit
            params = [ts_query, req.limit * 2]
            filters = ["o.tsv @@ to_tsquery('english', $1)"]
            pidx = 3

            if req.project and not req.cross_project:
                filters.append(f"p.name = ${pidx}")
                params.append(req.project)
                pidx += 1

            if req.type:
                placeholders = ", ".join(f"${pidx + i}" for i in range(len(req.type)))
                filters.append(f"o.type IN ({placeholders})")
                params.extend(req.type)
                pidx += len(req.type)

            where = " AND ".join(filters)

            fts_rows = await conn.fetch(f"""
                SELECT o.id, o.session_id, o.project_id, p.name as project_name,
                       o.title, o.subtitle, o.type, o.narrative,
                       o.facts, o.concepts, o.files_read, o.files_modified,
                       o.tool_name, o.prompt_number,
                       o.embedding IS NOT NULL as has_embedding,
                       o.created_at,
                       ts_rank_cd(o.tsv, to_tsquery('english', $1)) as fts_rank
                FROM mem_observations o
                JOIN mem_projects p ON p.id = o.project_id
                WHERE {where}
                ORDER BY fts_rank DESC
                LIMIT $2
            """, *params)

            for rank, row in enumerate(fts_rows):
                results.append((row["id"], 1.0 / (rank + 60), row))

        # RRF fusion: sum scores per observation id
        score_map: dict[int, tuple[float, dict]] = {}
        for obs_id, score, row in results:
            if obs_id in score_map:
                score_map[obs_id] = (score_map[obs_id][0] + score, score_map[obs_id][1])
            else:
                score_map[obs_id] = (score, row)

        # Apply recency boost: recent observations score higher
        import math
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        for obs_id, (score, row) in score_map.items():
            created = row["created_at"]
            if created:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = max((now_utc - created).total_seconds() / 86400, 0)
                # Exponential decay: today=2x, 7d=1.5x, 30d=1.1x, 90d+=1.0x
                boost = 1.0 + math.exp(-age_days / 10.0)
                score_map[obs_id] = (score * boost, row)

        # Sort by combined score
        ranked = sorted(score_map.values(), key=lambda x: x[0], reverse=True)[:req.limit]

        observations = []
        for score, row in ranked:
            obs = _row_to_obs(row)
            obs.score = round(score, 4)
            observations.append(obs)

        return SearchResult(
            observations=observations,
            query=req.query,
            mode=req.mode,
            total=len(observations),
        )


# ── Helpers ───────────────────────────────────────────

def _row_to_obs(row) -> ObservationOut:
    """Convert a database row to ObservationOut."""
    return ObservationOut(
        id=row["id"],
        session_id=row["session_id"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        title=row["title"],
        subtitle=row["subtitle"],
        type=row["type"],
        narrative=row["narrative"],
        facts=json.loads(row["facts"]) if isinstance(row["facts"], str) else (row["facts"] or []),
        concepts=json.loads(row["concepts"]) if isinstance(row["concepts"], str) else (row["concepts"] or []),
        files_read=json.loads(row["files_read"]) if isinstance(row["files_read"], str) else (row["files_read"] or []),
        files_modified=json.loads(row["files_modified"]) if isinstance(row["files_modified"], str) else (row["files_modified"] or []),
        tool_name=row["tool_name"],
        prompt_number=row["prompt_number"],
        has_embedding=row["has_embedding"],
        created_at=row["created_at"],
    )


def _build_raw_text(obs: ObservationCreate) -> str:
    """Build raw text from observation fields."""
    parts = [obs.title]
    if obs.subtitle:
        parts.append(obs.subtitle)
    if obs.narrative:
        parts.append(obs.narrative)
    for fact in obs.facts:
        parts.append(f"- {fact}")
    return "\n".join(parts)
