"""scripts/condense_lessons.py — review-gated backfill (issue #75).

Runs against a throwaway DB with every migration applied. The condenser is
stubbed; the contract under test is the review/apply/validate workflow.
"""

import importlib.util
import json
from pathlib import Path

import asyncpg
import pytest

from app import lesson_condense
from app.migrate import run_migrations

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "condense_lessons.py"
_spec = importlib.util.spec_from_file_location("condense_lessons", _SCRIPT)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

LONG_A = "Story about an outage. " * 20 + "Always run X first."
LONG_B = "Another long incident narrative. " * 15 + "Never do Y."


@pytest.fixture(scope="module")
async def seeded_db(throwaway_db: str) -> str:
    await run_migrations(throwaway_db)
    conn = await asyncpg.connect(throwaway_db)
    try:
        for title, rule, active in (
            ("a-long", LONG_A, True),
            ("b-long", LONG_B, True),
            ("c-inactive-long", LONG_B, False),
            ("d-short", "Short rule.", True),
        ):
            await conn.execute(
                "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool,"
                " raw_text, active, legacy_long_rule)"
                " VALUES ($1, $2, 'critical', 'input', 'Bash', $1, $3, $4)",
                title, rule, active, len(rule) > 280,
            )
    finally:
        await conn.close()
    return throwaway_db


@pytest.fixture
def stub_condenser(monkeypatch):
    async def fake(rule: str, detail=None):
        return lesson_condense.PreparedRule(
            rule=f"Condensed: {rule[-20:]}", detail=rule, provider="anvil:mlx:stub"
        )
    monkeypatch.setattr(cl, "prepare_rule", fake)


async def test_dry_run_writes_review_rows_for_active_long_lessons(seeded_db, stub_condenser, tmp_path):
    conn = await asyncpg.connect(seeded_db)
    try:
        rows = await cl.build_review(conn)
    finally:
        await conn.close()
    titles = sorted(r["title"] for r in rows)
    assert titles == ["a-long", "b-long"]
    for r in rows:
        assert r["new_length"] == len(r["new_rule"]) <= 280
        assert r["old_length"] == len(r["old_rule"]) > 280
        assert r["provider"] == "anvil:mlx:stub"

    path = cl.write_review(rows, tmp_path / "review.json")
    data = json.loads(path.read_text())
    assert data["count"] == 2
    assert {"id", "title", "old_rule", "new_rule", "new_length"} <= set(data["lessons"][0])
    assert path.with_suffix(".md").exists()


async def test_dry_run_does_not_write(seeded_db, stub_condenser):
    conn = await asyncpg.connect(seeded_db)
    try:
        await cl.build_review(conn)
        n = await conn.fetchval("SELECT count(*) FROM mem_lessons WHERE detail IS NOT NULL")
    finally:
        await conn.close()
    assert n == 0


async def test_apply_refuses_stale_review(seeded_db, stub_condenser, tmp_path):
    conn = await asyncpg.connect(seeded_db)
    try:
        rows = await cl.build_review(conn)
        rows[0]["old_rule"] = rows[0]["old_rule"] + " (edited since review)"
        with pytest.raises(cl.ReviewMismatch):
            await cl.apply_review(conn, rows)
        # Nothing applied, no backup left behind by the rolled-back transaction.
        assert await conn.fetchval("SELECT count(*) FROM mem_lessons WHERE detail IS NOT NULL") == 0
    finally:
        await conn.close()


async def test_apply_refuses_over_long_reviewed_rule(seeded_db, stub_condenser):
    conn = await asyncpg.connect(seeded_db)
    try:
        rows = await cl.build_review(conn)
        rows[0]["new_rule"] = "z" * 281
        with pytest.raises(ValueError):
            await cl.apply_review(conn, rows)
    finally:
        await conn.close()


async def test_apply_backs_up_then_applies_exactly_reviewed_rows(seeded_db, stub_condenser):
    conn = await asyncpg.connect(seeded_db)
    try:
        rows = await cl.build_review(conn)
        reviewed = [r for r in rows if r["title"] == "a-long"]  # operator dropped b-long
        backup = await cl.apply_review(conn, reviewed)
        assert backup.startswith("mem_lessons_backup_")
        assert await conn.fetchval(f'SELECT count(*) FROM "{backup}"') == 4
        assert await conn.fetchval(f"SELECT rule FROM \"{backup}\" WHERE title = 'a-long'") == LONG_A

        a = await conn.fetchrow("SELECT rule, detail, legacy_long_rule FROM mem_lessons WHERE title='a-long'")
        assert a["rule"] == reviewed[0]["new_rule"]
        assert a["detail"] == LONG_A
        assert a["legacy_long_rule"] is False
        b = await conn.fetchrow("SELECT rule, detail FROM mem_lessons WHERE title='b-long'")
        assert b["rule"] == LONG_B and b["detail"] is None
    finally:
        await conn.close()


async def test_validate_constraint_marks_it_valid(seeded_db):
    conn = await asyncpg.connect(seeded_db)
    try:
        await cl.validate_constraint(conn)
        valid = await conn.fetchval(
            "SELECT convalidated FROM pg_constraint WHERE conname = $1", cl.CONSTRAINT
        )
    finally:
        await conn.close()
    assert valid is True
