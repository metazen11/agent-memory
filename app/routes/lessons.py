import json
import logging
import re

from fastapi import APIRouter, HTTPException, Query

from app.db import get_pool
from app.embeddings import embed_text
from app.models import LessonCreate, LessonUpdate, LessonOut, LessonMatch

MAX_PATTERN_LEN = 500

logger = logging.getLogger(__name__)

router = APIRouter()


async def _ensure_project(conn, name: str) -> int:
    """Get or create a project by name. Returns project id."""
    row = await conn.fetchrow("SELECT id FROM mem_projects WHERE name = $1", name)
    if row:
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO mem_projects (name) VALUES ($1) RETURNING id", name
    )
    return row["id"]


def _row_to_lesson(row) -> LessonOut:
    return LessonOut(
        id=row["id"],
        project_id=row["project_id"],
        project_name=row.get("project_name"),
        title=row["title"],
        rule=row["rule"],
        severity=row["severity"],
        trigger_tool=row["trigger_tool"],
        trigger_pattern=row["trigger_pattern"],
        source_observation_id=row["source_observation_id"],
        trigger_count=row["trigger_count"],
        last_triggered_at=row["last_triggered_at"],
        active=row["active"],
        created_at=row["created_at"],
    )


# ── Create lesson ────────────────────────────────────

def _validate_pattern(pattern: str | None) -> None:
    """Validate trigger_pattern is a valid regex and not too long."""
    if pattern is None:
        return
    if len(pattern) > MAX_PATTERN_LEN:
        raise HTTPException(status_code=400, detail=f"trigger_pattern too long (max {MAX_PATTERN_LEN})")
    try:
        re.compile(pattern)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid trigger_pattern regex: {e}")


@router.post("/api/lessons", response_model=LessonOut)
async def create_lesson(lesson: LessonCreate):
    _validate_pattern(lesson.trigger_pattern)
    pool = await get_pool()
    async with pool.acquire() as conn:
        project_id = None
        project_name = None
        if lesson.project:
            project_id = await _ensure_project(conn, lesson.project)
            project_name = lesson.project

        raw_text = f"{lesson.title}\n{lesson.rule}"

        # Generate embedding
        embedding_str = None
        try:
            embedding = await embed_text(raw_text)
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
        except Exception as e:
            logger.warning(f"Embedding failed for lesson: {e}")

        row = await conn.fetchrow("""
            INSERT INTO mem_lessons (
                project_id, title, rule, severity,
                trigger_tool, trigger_pattern, source_observation_id,
                embedding, raw_text
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector, $9)
            RETURNING id, project_id, title, rule, severity,
                      trigger_tool, trigger_pattern, source_observation_id,
                      trigger_count, last_triggered_at, active, created_at
        """,
            project_id, lesson.title, lesson.rule, lesson.severity,
            lesson.trigger_tool, lesson.trigger_pattern, lesson.source_observation_id,
            embedding_str, raw_text,
        )

        return LessonOut(
            **{k: row[k] for k in row.keys()},
            project_name=project_name,
        )


# ── List lessons ─────────────────────────────────────

@router.get("/api/lessons", response_model=list[LessonOut])
async def list_lessons(
    project: str | None = None,
    severity: str | None = None,
    active: bool | None = True,
    limit: int = Query(default=20, le=100),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        pidx = 1

        if project is not None:
            conditions.append(f"p.name = ${pidx}")
            params.append(project)
            pidx += 1

        if severity is not None:
            conditions.append(f"l.severity = ${pidx}")
            params.append(severity)
            pidx += 1

        if active is not None:
            conditions.append(f"l.active = ${pidx}")
            params.append(active)
            pidx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        params.append(limit)
        rows = await conn.fetch(f"""
            SELECT l.*, p.name as project_name
            FROM mem_lessons l
            LEFT JOIN mem_projects p ON p.id = l.project_id
            {where}
            ORDER BY
                CASE l.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                l.created_at DESC
            LIMIT ${pidx}
        """, *params)

        return [_row_to_lesson(r) for r in rows]


# ── Match lessons for PreToolUse hook ────────────────

@router.get("/api/lessons/match", response_model=list[LessonMatch])
async def match_lessons(
    tool_name: str = Query(...),
    tool_input_preview: str = Query(default=""),
    project: str | None = None,
):
    """Match active lessons for a tool call. Used by PreToolUse hook.
    Returns max 5 lessons, critical first. Must be fast (<50ms)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Fetch candidate lessons: active, matching tool (or NULL = any tool),
        # scoped to project or global (project_id IS NULL)
        conditions = ["l.active = true"]
        params = []
        pidx = 1

        # Tool match: trigger_tool is NULL (matches any) OR matches the tool name
        conditions.append(f"(l.trigger_tool IS NULL OR l.trigger_tool = ${pidx})")
        params.append(tool_name)
        pidx += 1

        # Project scope: match project OR global lessons
        if project:
            conditions.append(f"(l.project_id IS NULL OR p.name = ${pidx})")
            params.append(project)
            pidx += 1
        else:
            conditions.append("l.project_id IS NULL")

        where = "WHERE " + " AND ".join(conditions)

        rows = await conn.fetch(f"""
            SELECT l.id, l.title, l.rule, l.severity, l.trigger_pattern,
                   l.trigger_count, p.name as project_name
            FROM mem_lessons l
            LEFT JOIN mem_projects p ON p.id = l.project_id
            {where}
            ORDER BY
                CASE l.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END
            LIMIT 20
        """, *params)

        # Filter by trigger_pattern regex match against tool_input_preview
        matches = []
        for row in rows:
            pattern = row["trigger_pattern"]
            if pattern:
                try:
                    if not re.search(pattern, tool_input_preview, re.IGNORECASE):
                        continue
                except re.error:
                    logger.warning(f"Invalid regex in lesson {row['id']}: {pattern}")
                    continue

            matches.append(LessonMatch(
                id=row["id"],
                title=row["title"],
                rule=row["rule"],
                severity=row["severity"],
                project_name=row["project_name"],
                trigger_count=row["trigger_count"],
            ))

            if len(matches) >= 5:
                break

        return matches


# ── Update lesson ────────────────────────────────────

@router.patch("/api/lessons/{lesson_id}", response_model=LessonOut)
async def update_lesson(lesson_id: int, update: LessonUpdate):
    _validate_pattern(update.trigger_pattern)
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Build dynamic SET clause
        sets = []
        params = []
        pidx = 1

        for field in ("title", "rule", "severity", "trigger_tool", "trigger_pattern", "active"):
            value = getattr(update, field)
            if value is not None:
                sets.append(f"{field} = ${pidx}")
                params.append(value)
                pidx += 1

        if not sets:
            raise HTTPException(status_code=400, detail="No fields to update")

        # Re-embed if title or rule changed
        if update.title is not None or update.rule is not None:
            # Fetch current values for fields not being updated
            current = await conn.fetchrow(
                "SELECT title, rule FROM mem_lessons WHERE id = $1", lesson_id
            )
            if not current:
                raise HTTPException(status_code=404, detail="Lesson not found")

            new_title = update.title or current["title"]
            new_rule = update.rule or current["rule"]
            raw_text = f"{new_title}\n{new_rule}"

            try:
                embedding = await embed_text(raw_text)
                emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
                sets.append(f"embedding = ${pidx}::vector")
                params.append(emb_str)
                pidx += 1
                sets.append(f"raw_text = ${pidx}")
                params.append(raw_text)
                pidx += 1
            except Exception as e:
                logger.warning(f"Re-embedding failed: {e}")

        params.append(lesson_id)
        row = await conn.fetchrow(f"""
            UPDATE mem_lessons
            SET {", ".join(sets)}
            WHERE id = ${pidx}
            RETURNING *
        """, *params)

        if not row:
            raise HTTPException(status_code=404, detail="Lesson not found")

        # Fetch project name
        project_name = None
        if row["project_id"]:
            p = await conn.fetchrow("SELECT name FROM mem_projects WHERE id = $1", row["project_id"])
            if p:
                project_name = p["name"]

        return LessonOut(
            **{k: row[k] for k in row.keys() if k not in ("embedding", "raw_text", "tsv")},
            project_name=project_name,
        )


# ── Trigger tracking ─────────────────────────────────

@router.post("/api/lessons/{lesson_id}/trigger")
async def trigger_lesson(lesson_id: int):
    """Log that a lesson was triggered (fire-and-forget from hook)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE mem_lessons
            SET trigger_count = trigger_count + 1,
                last_triggered_at = now()
            WHERE id = $1
        """, lesson_id)
    return {"triggered": True}
