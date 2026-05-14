"""Migration 012 — reverse + re-apply cycle.

Pattern: apply, run the ``.down.sql`` script, verify columns/indexes/FK are
gone and the tracking row is removed, then re-apply via the runner and
verify everything is back.
"""

from pathlib import Path

import asyncpg
import pytest

from app.migrate import run_migrations

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOWN_SQL = (
    _REPO_ROOT / "scripts" / "migrations" / "012-v2-data-pipeline.down.sql"
)


async def _column_names(conn: asyncpg.Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {r["column_name"] for r in rows}


async def _index_names(conn: asyncpg.Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = $1",
        table,
    )
    return {r["indexname"] for r in rows}


async def _has_constraint(
    conn: asyncpg.Connection, table: str, conname: str
) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = $1 AND c.conname = $2
            """,
            table,
            conname,
        )
    )


async def _tracking_rows(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT filename FROM mem_schema_migrations WHERE filename LIKE '012-%'"
    )
    return sorted(r["filename"] for r in rows)


@pytest.fixture
async def fresh_applied(throwaway_db: str) -> str:
    """Apply all migrations (including 012) on a fresh DB for each test."""
    # The throwaway DB is module-scoped, so we manually drop the 012 schema
    # objects if a prior test left them in either state, then re-apply.
    await run_migrations(throwaway_db)
    return throwaway_db


async def test_reverse_then_reapply(fresh_applied: str):
    dsn = fresh_applied

    # --- Baseline: 012 is applied. ---
    conn = await asyncpg.connect(dsn)
    try:
        cols_tc = await _column_names(conn, "mem_tool_calls")
        cols_up = await _column_names(conn, "mem_user_prompts")
        cols_proj = await _column_names(conn, "mem_projects")
        idx = await _index_names(conn, "mem_tool_calls")
        has_fk = await _has_constraint(
            conn, "mem_tool_calls", "mem_tool_calls_prev_user_prompt_fk"
        )
        tracking = await _tracking_rows(conn)
    finally:
        await conn.close()

    assert "prev_user_prompt_id" in cols_tc
    assert "git_remote" in cols_proj
    assert "turn_index" in cols_up
    assert "mem_tool_calls_prev_user_prompt_id_idx" in idx
    assert "mem_tool_calls_session_turn_idx" in idx
    assert has_fk
    assert tracking == [
        "012-v2-data-pipeline.concurrent.sql",
        "012-v2-data-pipeline.sql",
    ]

    # --- Apply the down migration via psql-style raw exec. ---
    # Reuse the production SQL splitter from app.migrate so this test
    # exercises the same parse path as the runner (and doesn't reinvent it).
    from app.migrate import _split_sql_statements
    down_sql = _DOWN_SQL.read_text()
    conn = await asyncpg.connect(dsn)
    try:
        for stmt in _split_sql_statements(down_sql):
            await conn.execute(stmt)
    finally:
        await conn.close()

    # --- Verify reversal removed everything. ---
    conn = await asyncpg.connect(dsn)
    try:
        cols_tc = await _column_names(conn, "mem_tool_calls")
        cols_up = await _column_names(conn, "mem_user_prompts")
        cols_proj = await _column_names(conn, "mem_projects")
        idx = await _index_names(conn, "mem_tool_calls")
        has_fk = await _has_constraint(
            conn, "mem_tool_calls", "mem_tool_calls_prev_user_prompt_fk"
        )
        tracking = await _tracking_rows(conn)
    finally:
        await conn.close()

    for col in (
        "turn_index",
        "turn_subindex",
        "prev_user_prompt_id",
        "backfill_run_id",
        "retention_class",
        "content_hash",
        "truncated_at_bytes",
    ):
        assert col not in cols_tc, f"mem_tool_calls.{col} should be dropped"
    for col in ("retention_class", "backfill_run_id", "turn_index", "content_hash"):
        assert col not in cols_up, f"mem_user_prompts.{col} should be dropped"
    assert "git_remote" not in cols_proj
    assert "mem_tool_calls_prev_user_prompt_id_idx" not in idx
    assert "mem_tool_calls_session_turn_idx" not in idx
    assert not has_fk
    assert tracking == []

    # --- Re-apply via the runner. ---
    applied = await run_migrations(dsn)
    assert "012-v2-data-pipeline.sql" in applied
    assert "012-v2-data-pipeline.concurrent.sql" in applied

    # --- Verify everything is back. ---
    conn = await asyncpg.connect(dsn)
    try:
        cols_tc = await _column_names(conn, "mem_tool_calls")
        cols_proj = await _column_names(conn, "mem_projects")
        idx = await _index_names(conn, "mem_tool_calls")
        has_fk = await _has_constraint(
            conn, "mem_tool_calls", "mem_tool_calls_prev_user_prompt_fk"
        )
    finally:
        await conn.close()

    assert "prev_user_prompt_id" in cols_tc
    assert "git_remote" in cols_proj
    assert "mem_tool_calls_prev_user_prompt_id_idx" in idx
    assert "mem_tool_calls_session_turn_idx" in idx
    assert has_fk
