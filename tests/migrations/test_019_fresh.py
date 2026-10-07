"""Migration 019 on a database with no over-long rules (fresh installs, CI):
the end state is reached immediately — a validated CHECK and no trigger."""

import asyncpg

from app.migrate import run_migrations


async def test_fresh_db_gets_validated_check_and_no_trigger(throwaway_db):
    await run_migrations(throwaway_db)
    conn = await asyncpg.connect(throwaway_db)
    try:
        validated = await conn.fetchval(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'chk_lesson_rule_len'"
        )
        triggers = await conn.fetchval(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_mem_lessons_rule_cap'"
        )
    finally:
        await conn.close()
    assert validated is True
    assert triggers == 0
