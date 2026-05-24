import fnmatch
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.db import get_pool
from app.embeddings import embed_text
from app.models import LessonCreate, LessonUpdate, LessonOut, LessonMatch
from app.project import ensure_project, project_path_filter, project_path_filter_strict

MAX_PATTERN_LEN = 500
VALID_TRIGGER_ON = ("input", "output", "phase", "file_scope")
VALID_TRIGGER_PHASES = ("pre_tool", "post_tool", "pre_response", "session_end")

logger = logging.getLogger(__name__)

router = APIRouter()


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
        trigger_on=row.get("trigger_on", "input"),
        trigger_output_pattern=row.get("trigger_output_pattern"),
        trigger_phase=row.get("trigger_phase"),
        trigger_files=row.get("trigger_files"),
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


def _validate_trigger_on(lesson: LessonCreate) -> None:
    """Validate trigger_on value and required companion fields."""
    if lesson.trigger_on not in VALID_TRIGGER_ON:
        raise HTTPException(
            status_code=400,
            detail=f"trigger_on must be one of {VALID_TRIGGER_ON}",
        )
    if lesson.trigger_on == "input" and not lesson.trigger_tool and not lesson.trigger_pattern:
        # Refuses broad-match input lessons (no tool AND no pattern) — they
        # would fire on every Edit/Write/Bash/NotebookEdit call and dominate
        # the systemMessage budget. DB CHECK constraint chk_input_trigger_has_filter
        # (migration 016) is the belt; this is the suspenders that returns a
        # clean 400 instead of letting the DB error surface as a 500.
        raise HTTPException(
            status_code=400,
            detail="trigger_tool or trigger_pattern required when trigger_on='input' (a lesson without either would match every tool call)",
        )
    if lesson.trigger_on == "output" and not lesson.trigger_output_pattern:
        raise HTTPException(
            status_code=400,
            detail="trigger_output_pattern required when trigger_on='output'",
        )
    if lesson.trigger_on == "phase":
        if not lesson.trigger_phase:
            raise HTTPException(
                status_code=400,
                detail="trigger_phase required when trigger_on='phase'",
            )
        if lesson.trigger_phase not in VALID_TRIGGER_PHASES:
            raise HTTPException(
                status_code=400,
                detail=f"trigger_phase must be one of {VALID_TRIGGER_PHASES}",
            )
    if lesson.trigger_on == "file_scope" and not lesson.trigger_files:
        raise HTTPException(
            status_code=400,
            detail="trigger_files required when trigger_on='file_scope'",
        )


@router.post("/api/lessons", response_model=LessonOut)
async def create_lesson(lesson: LessonCreate):
    _validate_pattern(lesson.trigger_pattern)
    _validate_pattern(lesson.trigger_output_pattern)
    _validate_trigger_on(lesson)
    pool = await get_pool()
    async with pool.acquire() as conn:
        project_id = None
        project_name = None
        if lesson.project:
            project_id = await ensure_project(conn, lesson.project)
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
                embedding, raw_text,
                trigger_on, trigger_output_pattern, trigger_phase, trigger_files
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector, $9, $10, $11, $12, $13)
            RETURNING id, project_id, title, rule, severity,
                      trigger_tool, trigger_pattern, source_observation_id,
                      trigger_count, last_triggered_at, active, created_at,
                      trigger_on, trigger_output_pattern, trigger_phase, trigger_files
        """,
            project_id, lesson.title, lesson.rule, lesson.severity,
            lesson.trigger_tool, lesson.trigger_pattern, lesson.source_observation_id,
            embedding_str, raw_text,
            lesson.trigger_on, lesson.trigger_output_pattern, lesson.trigger_phase,
            lesson.trigger_files,
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

        # Explicit scoping semantics:
        #   project=<path>  → lessons attached to a project matching that path
        #                     (bidirectional prefix + basename fallback), OR
        #                     truly unscoped lessons (project_id IS NULL).
        #   project=None    → ONLY truly unscoped lessons (project_id IS NULL).
        # Before this change, project=None returned every lesson regardless of
        # scope, which leaked other-project lessons into the session-start /
        # user-prompt-submit injections.
        if project is not None:
            basename = Path(project).name or project
            clause, pidx = project_path_filter(pidx)
            conditions.append(
                f"(l.project_id IS NULL OR {clause} OR p.name = ${pidx})"
            )
            params.extend([project, project, project, basename])
            pidx += 1
        else:
            conditions.append("l.project_id IS NULL")

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
    trigger_on: str = Query(default="input"),
    tool_output_preview: str = Query(default=""),
    trigger_phase: str | None = None,
    modified_files: str = Query(default=""),
):
    """Match active lessons for a tool/lifecycle event.

    trigger_on controls which code path runs:
    - input (default): regex trigger_pattern against tool_input_preview (PreToolUse)
    - output: regex trigger_output_pattern against tool_output_preview (PostToolUse)
    - phase: match trigger_phase exactly (pre_response, session_end, etc.)
    - file_scope: fnmatch trigger_files globs against modified_files list

    Returns max 5 lessons, critical first. Must be fast (<50ms).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = ["l.active = true"]
        params = []
        pidx = 1

        # Filter by trigger_on type
        conditions.append(f"l.trigger_on = ${pidx}")
        params.append(trigger_on)
        pidx += 1

        # Tool match (skip for phase triggers — they aren't tool-specific)
        if trigger_on != "phase":
            conditions.append(f"(l.trigger_tool IS NULL OR l.trigger_tool = ${pidx})")
            params.append(tool_name)
            pidx += 1

        # Broad-match guard for input triggers: refuse to fire a lesson that
        # has neither a trigger_tool NOR a trigger_pattern. Such a lesson
        # would match every Edit/Write/Bash/NotebookEdit call and dominate
        # the systemMessage budget. The DB has a CHECK constraint that
        # prevents new rows in this state (migration 016), but this filter
        # is the runtime safety net for legacy rows that pre-date it. The
        # output/file_scope/phase trigger types have their own
        # required-field CHECK constraints already.
        if trigger_on == "input":
            conditions.append(
                "(l.trigger_tool IS NOT NULL OR l.trigger_pattern IS NOT NULL)"
            )

        # Project scope: strict one-directional match. A lesson fires only
        # when the caller's cwd IS the lesson's project path or is nested
        # inside it. Truly unscoped lessons (project_id IS NULL) still fire.
        # Parent-cwd does NOT match child-project lessons — that's the leak
        # this endpoint had before, where /Users/mz pulled in lessons from
        # every project under /Users/mz/_CODING/*.
        if project:
            path_clause, pidx = project_path_filter_strict(pidx)
            conditions.append(f"(l.project_id IS NULL OR {path_clause})")
            params.extend([project, project])
        else:
            conditions.append("l.project_id IS NULL")

        # Phase-specific: also filter by trigger_phase in SQL
        if trigger_on == "phase" and trigger_phase:
            conditions.append(f"l.trigger_phase = ${pidx}")
            params.append(trigger_phase)
            pidx += 1

        where = "WHERE " + " AND ".join(conditions)

        rows = await conn.fetch(f"""
            SELECT l.id, l.title, l.rule, l.severity,
                   l.trigger_pattern, l.trigger_output_pattern,
                   l.trigger_phase, l.trigger_files,
                   l.trigger_count, p.name as project_name
            FROM mem_lessons l
            LEFT JOIN mem_projects p ON p.id = l.project_id
            {where}
            ORDER BY
                CASE l.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END
            LIMIT 20
        """, *params)

        # Application-side filtering per trigger type
        matches = []
        modified_file_list = [f.strip() for f in modified_files.split(",") if f.strip()] if modified_files else []

        for row in rows:
            if trigger_on == "input":
                pattern = row["trigger_pattern"]
                if pattern:
                    try:
                        if not re.search(pattern, tool_input_preview, re.IGNORECASE):
                            continue
                    except re.error:
                        logger.warning(f"Invalid regex in lesson {row['id']}: {pattern}")
                        continue

            elif trigger_on == "output":
                pattern = row["trigger_output_pattern"]
                if pattern:
                    try:
                        if not re.search(pattern, tool_output_preview, re.IGNORECASE):
                            continue
                    except re.error:
                        logger.warning(f"Invalid regex in lesson {row['id']}: {pattern}")
                        continue

            elif trigger_on == "phase":
                pass  # already filtered by SQL

            elif trigger_on == "file_scope":
                globs = row["trigger_files"] or []
                if not _any_glob_matches(globs, modified_file_list):
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


def _any_glob_matches(globs: list[str], files: list[str]) -> bool:
    """Return True if any glob pattern matches any file path."""
    for glob_pat in globs:
        for filepath in files:
            if fnmatch.fnmatch(filepath, glob_pat):
                return True
            # Also try matching against just the filename
            if fnmatch.fnmatch(filepath.rsplit("/", 1)[-1], glob_pat):
                return True
    return False


# ── Update lesson ────────────────────────────────────

@router.patch("/api/lessons/{lesson_id}", response_model=LessonOut)
async def update_lesson(lesson_id: int, update: LessonUpdate):
    _validate_pattern(update.trigger_pattern)
    _validate_pattern(update.trigger_output_pattern)
    if update.trigger_on is not None and update.trigger_on not in VALID_TRIGGER_ON:
        raise HTTPException(status_code=400, detail=f"trigger_on must be one of {VALID_TRIGGER_ON}")
    if update.trigger_phase is not None and update.trigger_phase not in VALID_TRIGGER_PHASES:
        raise HTTPException(status_code=400, detail=f"trigger_phase must be one of {VALID_TRIGGER_PHASES}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Build dynamic SET clause
        sets = []
        params = []
        pidx = 1

        for field in (
            "title", "rule", "severity", "trigger_tool", "trigger_pattern", "active",
            "trigger_on", "trigger_output_pattern", "trigger_phase",
        ):
            value = getattr(update, field)
            if value is not None:
                sets.append(f"{field} = ${pidx}")
                params.append(value)
                pidx += 1

        # trigger_files is a list — handle separately
        if update.trigger_files is not None:
            sets.append(f"trigger_files = ${pidx}")
            params.append(update.trigger_files)
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
