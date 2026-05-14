"""Migration 012 — v2 data pipeline schema additions.

Verifies the ``up`` direction:
- All new columns exist on the expected tables.
- Both indexes exist on ``mem_tool_calls``.
- The ``prev_user_prompt_id`` FK exists AND is validated (not NOT VALID).
- Tracking-table rows recorded for both the base and concurrent files.
- Re-running the runner is a no-op (idempotency).

The ``down`` and apply-after-down cycle is exercised in
``test_012_reverse.py``.
"""

from pathlib import Path

import asyncpg
import pytest

from app.migrate import run_migrations

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
async def applied_db(throwaway_db: str) -> str:
    """Apply ALL migrations (001..012) once per module."""
    await run_migrations(throwaway_db)
    return throwaway_db


async def _columns(conn: asyncpg.Connection, table: str) -> dict[str, dict]:
    """Return ``{column_name: {data_type, is_nullable, column_default}}``."""
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        """,
        table,
    )
    return {
        r["column_name"]: {
            "data_type": r["data_type"],
            "is_nullable": r["is_nullable"],
            "column_default": r["column_default"],
        }
        for r in rows
    }


async def _index_names(conn: asyncpg.Connection, table: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = $1
        """,
        table,
    )
    return {r["indexname"] for r in rows}


async def _constraint(
    conn: asyncpg.Connection, table: str, conname: str
) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT c.conname, c.contype, c.convalidated, c.confdeltype
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = $1 AND c.conname = $2
        """,
        table,
        conname,
    )
    if row is None:
        return None
    return {
        "conname": row["conname"],
        "contype": row["contype"],
        "convalidated": row["convalidated"],
        "confdeltype": row["confdeltype"],
    }


class TestMigration012Schema:
    """Schema-level assertions about migration 012."""

    async def test_mem_projects_git_remote_added(self, applied_db: str):
        conn = await asyncpg.connect(applied_db)
        try:
            cols = await _columns(conn, "mem_projects")
        finally:
            await conn.close()
        assert "git_remote" in cols
        assert cols["git_remote"]["data_type"] == "text"
        assert cols["git_remote"]["is_nullable"] == "YES"

    async def test_mem_tool_calls_columns_added(self, applied_db: str):
        conn = await asyncpg.connect(applied_db)
        try:
            cols = await _columns(conn, "mem_tool_calls")
        finally:
            await conn.close()

        expected = {
            "turn_index": ("integer", "YES", None),
            "turn_subindex": ("integer", "YES", None),
            "prev_user_prompt_id": ("bigint", "YES", None),
            "backfill_run_id": ("text", "YES", None),
            "retention_class": ("text", "YES", "'live'::text"),
            "content_hash": ("text", "YES", None),
            "truncated_at_bytes": ("integer", "YES", None),
        }
        for name, (dtype, nullable, default) in expected.items():
            assert name in cols, f"missing column {name}"
            assert cols[name]["data_type"] == dtype, f"{name} type mismatch"
            assert cols[name]["is_nullable"] == nullable, f"{name} nullability"
            assert cols[name]["column_default"] == default, f"{name} default"

    async def test_mem_user_prompts_columns_added(self, applied_db: str):
        conn = await asyncpg.connect(applied_db)
        try:
            cols = await _columns(conn, "mem_user_prompts")
        finally:
            await conn.close()

        expected = {
            "retention_class": ("text", "YES", "'live'::text"),
            "backfill_run_id": ("text", "YES", None),
            "turn_index": ("integer", "YES", None),
            "content_hash": ("text", "YES", None),
        }
        for name, (dtype, nullable, default) in expected.items():
            assert name in cols, f"missing column {name}"
            assert cols[name]["data_type"] == dtype, f"{name} type mismatch"
            assert cols[name]["is_nullable"] == nullable, f"{name} nullability"
            assert cols[name]["column_default"] == default, f"{name} default"

    async def test_indexes_created(self, applied_db: str):
        conn = await asyncpg.connect(applied_db)
        try:
            idx = await _index_names(conn, "mem_tool_calls")
        finally:
            await conn.close()
        assert "mem_tool_calls_prev_user_prompt_id_idx" in idx
        assert "mem_tool_calls_session_turn_idx" in idx

    async def test_fk_validated(self, applied_db: str):
        """FK is added NOT VALID then validated by the concurrent section."""
        conn = await asyncpg.connect(applied_db)
        try:
            con = await _constraint(
                conn, "mem_tool_calls", "mem_tool_calls_prev_user_prompt_fk"
            )
        finally:
            await conn.close()
        assert con is not None, "FK constraint missing"
        # pg_constraint.contype/confdeltype come back as single-byte values via
        # asyncpg's char codec, so compare against bytes literals (b'f', b'n').
        assert con["contype"] == b"f", "constraint should be a foreign key"
        # convalidated TRUE means VALIDATE CONSTRAINT ran successfully.
        assert con["convalidated"] is True, "FK should be validated"
        # 'n' = SET NULL on delete
        assert con["confdeltype"] == b"n", "ON DELETE should be SET NULL"

    async def test_fk_enforces_referential_integrity(self, applied_db: str):
        """Inserting a tool_call with a bogus prev_user_prompt_id fails."""
        conn = await asyncpg.connect(applied_db)
        try:
            # Seed minimum project + session.
            project_id = await conn.fetchval(
                "INSERT INTO mem_projects(name, full_path) "
                "VALUES('p012', '/tmp/p012') RETURNING id"
            )
            session_id = await conn.fetchval(
                "INSERT INTO mem_sessions(project_id, session_id) "
                "VALUES($1, 'sess-012') RETURNING id",
                project_id,
            )
            # FK should reject prev_user_prompt_id = 999999 (no such row).
            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO mem_tool_calls(session_id, project_id, "
                    "prev_user_prompt_id) VALUES($1, $2, 999999)",
                    session_id,
                    project_id,
                )
        finally:
            await conn.close()

    async def test_tracking_rows_recorded(self, applied_db: str):
        conn = await asyncpg.connect(applied_db)
        try:
            rows = await conn.fetch(
                "SELECT filename FROM mem_schema_migrations "
                "WHERE filename LIKE '012-%' ORDER BY id"
            )
        finally:
            await conn.close()
        names = [r["filename"] for r in rows]
        assert "012-v2-data-pipeline.sql" in names
        assert "012-v2-data-pipeline.concurrent.sql" in names


class TestMigration012Idempotency:
    """Re-running the runner against an up-to-date DB is a no-op."""

    async def test_rerun_is_noop(self, applied_db: str):
        # The fixture already ran migrations once. A second run must return
        # an empty list (no migrations applied) without erroring.
        result = await run_migrations(applied_db)
        assert result == [], (
            "second run should be a no-op; got pending migrations: "
            f"{result}"
        )
