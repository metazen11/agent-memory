"""Migration 019 — lesson `detail` column + 280-char rule cap (issue #75).

Pins:
- `detail` exists and is nullable; full-text search indexes it.
- While pre-019 long rules still exist, a BEFORE INSERT/UPDATE trigger
  enforces the cap on every write that could introduce a long active rule:
  any INSERT, any change of `rule`, reactivation of a long row, and
  promotion of a long row to `critical`. Other
  UPDATEs on a long legacy row (trigger_count, deactivation) keep working.
  A bare `CHECK ... NOT VALID` cannot do this: Postgres checks every new
  row version on UPDATE, so counter updates on legacy rows would fail.
- There is no writable escape hatch (an earlier draft used a
  `legacy_long_rule` flag that any INSERT/UPDATE could set to true).
- `mem_lessons_finalize_rule_cap()` replaces the trigger with a validated
  `CHECK (char_length(rule) <= 280)` once no long rows remain, and refuses
  while any do.
"""

import asyncpg
import pytest

from app.migrate import run_migrations

from .conftest import apply_migrations_before

LONG = "L" * 900


@pytest.fixture(scope="module")
async def pre019_db(throwaway_db: str) -> str:
    """Apply 001..018, seed legacy long rules, then apply 019."""
    await apply_migrations_before(throwaway_db, 19)
    conn = await asyncpg.connect(throwaway_db)
    try:
        for title, active, severity in (
            ("legacy", True, "critical"),
            ("legacy-inactive", False, "critical"),
            ("legacy-2", True, "warning"),
        ):
            await conn.execute(
                "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text, active)"
                " VALUES ($1, $2, $4, 'input', 'Bash', $1, $3)",
                title, LONG, active, severity,
            )
    finally:
        await conn.close()
    await run_migrations(throwaway_db)
    return throwaway_db


@pytest.fixture
async def conn(pre019_db):
    c = await asyncpg.connect(pre019_db)
    try:
        yield c
    finally:
        await c.close()


_INSERT = (
    "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
    " VALUES ($1, $2, 'warning', 'input', 'Bash', $1) RETURNING id"
)


async def test_detail_column_is_nullable_text(conn):
    row = await conn.fetchrow(
        "SELECT data_type, is_nullable FROM information_schema.columns"
        " WHERE table_name = 'mem_lessons' AND column_name = 'detail'"
    )
    assert row is not None
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"


async def test_db_refuses_new_rule_over_280(conn):
    await conn.execute(_INSERT, "ok-280", "x" * 280)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(_INSERT, "too-long", "x" * 281)


async def test_no_writable_grandfather_flag(conn):
    """RED against the flag design: INSERT ... legacy_long_rule=true was accepted."""
    exists = await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_name = 'mem_lessons' AND column_name = 'legacy_long_rule'"
    )
    assert exists == 0
    with pytest.raises((asyncpg.CheckViolationError, asyncpg.UndefinedColumnError)):
        await conn.execute(
            "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text,"
            " legacy_long_rule) VALUES ('smuggle', $1, 'info', 'input', 'Bash', 'x', true)",
            "x" * 281,
        )


async def test_update_to_long_rule_refused_on_short_row(conn):
    """RED against the flag design: SET rule=<400>, legacy_long_rule=true was accepted."""
    lesson_id = await conn.fetchval(_INSERT, "short-then-long", "short")
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE mem_lessons SET rule = $2 WHERE id = $1", lesson_id, "y" * 400)
    # ...including when the same statement tries to flip a grandfather flag.
    with pytest.raises((asyncpg.CheckViolationError, asyncpg.UndefinedColumnError)):
        await conn.execute(
            "UPDATE mem_lessons SET rule = $2, legacy_long_rule = true WHERE id = $1",
            lesson_id, "y" * 400,
        )


async def test_long_rule_write_on_legacy_row_refused(conn):
    """RED against the flag design: a flagged row accepted ANY new long rule."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE mem_lessons SET rule = $1 WHERE title = 'legacy'", "y" * 400)


async def test_reactivating_long_legacy_row_refused(conn):
    """RED against the flag design: PATCH {"active": true} revived a 600-char rule."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE mem_lessons SET active = true WHERE title = 'legacy-inactive'")


async def test_promoting_long_legacy_row_to_critical_refused(conn):
    """Active critical lessons are the injected set; promoting a long one
    recreates the truncated-instruction state (codex review)."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE mem_lessons SET severity = 'critical' WHERE title = 'legacy-2'")


async def test_legacy_long_row_still_accepts_counter_updates(conn):
    await conn.execute(
        "UPDATE mem_lessons SET trigger_count = trigger_count + 1 WHERE title = 'legacy'"
    )
    await conn.execute("UPDATE mem_lessons SET active = false WHERE title = 'legacy-2'")


async def test_full_text_search_finds_lesson_by_detail(conn):
    """Condensing moves the backstory into `detail`; FTS must still hit it."""
    await conn.execute(
        "INSERT INTO mem_lessons (title, rule, detail, severity, trigger_on, trigger_tool, raw_text)"
        " VALUES ('fts', 'Run the gate.', 'The zanzibarquokka outage happened first.',"
        " 'info', 'input', 'Bash', 'fts')"
    )
    hit = await conn.fetchval(
        "SELECT title FROM mem_lessons WHERE tsv @@ plainto_tsquery('english', 'zanzibarquokka')"
    )
    assert hit == "fts"


async def _cap_state(conn):
    check = await conn.fetchrow(
        "SELECT convalidated FROM pg_constraint WHERE conname = 'chk_lesson_rule_len'"
    )
    trigger = await conn.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_mem_lessons_rule_cap'"
    )
    return (check["convalidated"] if check else None), trigger


async def test_finalize_refuses_while_long_rows_remain(conn):
    assert await _cap_state(conn) == (None, 1)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("SELECT mem_lessons_finalize_rule_cap()")
    assert await _cap_state(conn) == (None, 1)


async def test_finalize_after_backfill_installs_validated_check(conn):
    # Simulate the backfill: condense every long row (active or not).
    await conn.execute(
        "UPDATE mem_lessons SET detail = rule, rule = 'Condensed.' WHERE char_length(rule) > 280"
    )
    await conn.execute("SELECT mem_lessons_finalize_rule_cap()")
    assert await _cap_state(conn) == (True, 0)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(_INSERT, "after-final", "z" * 281)
    await conn.execute("SELECT mem_lessons_finalize_rule_cap()")  # idempotent
