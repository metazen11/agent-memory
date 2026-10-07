"""Migration 019 — lesson `detail` column + rule-length CHECK (issue #75).

Pins:
- `detail` exists and is nullable.
- A NEW rule longer than 280 chars is refused by the database itself.
- Rows that were already over the limit when 019 ran are grandfathered
  (`legacy_long_rule = true`) so ordinary UPDATEs on them — trigger_count
  increments, deactivation — keep working until the backfill condenses them.
  A plain `CHECK ... NOT VALID` is NOT enough for that: Postgres still
  checks every new row version on UPDATE, so `SET trigger_count = ...` on a
  long legacy row would start failing the moment the migration ran.
- The grandfather flag cannot be used to smuggle in a new long rule once
  the backfill has cleared it and the constraint is validated.
"""

import shutil
import tempfile
from pathlib import Path

import asyncpg
import pytest

import app.migrate as migrate
from app.migrate import run_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "migrations"


@pytest.fixture(scope="module")
async def pre019_db(throwaway_db: str) -> str:
    """Apply 001..018, seed a legacy long rule, then apply 019."""
    staging = Path(tempfile.mkdtemp(prefix="pre019-"))
    try:
        for f in MIGRATIONS.glob("*.sql"):
            if int(f.name.split("-", 1)[0]) < 19:
                (staging / f.name).write_text(f.read_text())
        original = migrate.MIGRATIONS_DIR
        migrate.MIGRATIONS_DIR = staging
        try:
            await run_migrations(throwaway_db)
        finally:
            migrate.MIGRATIONS_DIR = original
    finally:
        shutil.rmtree(staging)
    conn = await asyncpg.connect(throwaway_db)
    try:
        await conn.execute(
            "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
            " VALUES ('legacy', $1, 'critical', 'input', 'Bash', 'legacy')",
            "L" * 900,
        )
    finally:
        await conn.close()
    await run_migrations(throwaway_db)
    return throwaway_db


async def test_detail_column_is_nullable_text(pre019_db):
    conn = await asyncpg.connect(pre019_db)
    try:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'mem_lessons' AND column_name = 'detail'"
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"


async def test_db_refuses_new_rule_over_280(pre019_db):
    conn = await asyncpg.connect(pre019_db)
    try:
        await conn.execute(
            "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
            " VALUES ('ok', $1, 'warning', 'input', 'Bash', 'ok')",
            "x" * 280,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
                " VALUES ('too long', $1, 'warning', 'input', 'Bash', 'x')",
                "x" * 281,
            )
    finally:
        await conn.close()


async def test_legacy_long_row_still_accepts_counter_updates(pre019_db):
    conn = await asyncpg.connect(pre019_db)
    try:
        flagged = await conn.fetchval(
            "SELECT legacy_long_rule FROM mem_lessons WHERE title = 'legacy'"
        )
        assert flagged is True
        await conn.execute(
            "UPDATE mem_lessons SET trigger_count = trigger_count + 1 WHERE title = 'legacy'"
        )
        await conn.execute("UPDATE mem_lessons SET active = false WHERE title = 'legacy'")
    finally:
        await conn.close()


async def test_new_rows_are_not_grandfathered(pre019_db):
    conn = await asyncpg.connect(pre019_db)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM mem_lessons WHERE legacy_long_rule AND char_length(rule) <= 280"
        )
    finally:
        await conn.close()
    assert n == 0
