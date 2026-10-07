"""The documented rollback for scripts/condense_lessons.py --apply (#75).

Runs the exact CLI commands the README prints, end to end, on a throwaway
DB whose long lessons pre-date migration 019.
"""

import importlib.util
import re
from pathlib import Path

import asyncpg
import pytest

from app import lesson_condense
from app.migrate import run_migrations

from .conftest import apply_migrations_before

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "condense_lessons.py"
_spec = importlib.util.spec_from_file_location("condense_lessons_rb", _SCRIPT)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)

LONG = "Backstory sentence. " * 30 + "Do X."


async def fake_embed(text: str) -> str:
    return "[" + ",".join(["0.2"] * 768) + "]"


@pytest.fixture(scope="module")
async def db(throwaway_db):
    await apply_migrations_before(throwaway_db, 19)
    conn = await asyncpg.connect(throwaway_db)
    try:
        await conn.execute(
            "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
            " VALUES ('rb', $1, 'critical', 'input', 'Bash', 'rb-raw')", LONG,
        )
    finally:
        await conn.close()
    await run_migrations(throwaway_db)
    return throwaway_db


@pytest.fixture
def stub(monkeypatch):
    async def fake(rule, detail=None):
        return lesson_condense.PreparedRule(rule="Do X.", detail=rule, provider="stub")
    monkeypatch.setattr(cl, "prepare_rule", fake)


async def test_documented_rollback_restores_and_keeps_trigger(db, stub, capsys):
    conn = await asyncpg.connect(db)
    try:
        rows = await cl.build_review(conn)
        backup = await cl.apply_review(conn, rows, embed=fake_embed)
    finally:
        await conn.close()
    # Unquoted identifiers fold to lower case; the printed name must survive that.
    assert re.fullmatch(r"mem_lessons_backup_[0-9a-z_]+", backup)

    code = await cl.main(["--rollback", backup, "--dsn", db])
    assert code == 0, capsys.readouterr().err

    conn = await asyncpg.connect(db)
    try:
        row = await conn.fetchrow("SELECT rule, detail, raw_text FROM mem_lessons WHERE title='rb'")
        enabled = await conn.fetchval(
            "SELECT tgenabled::text FROM pg_trigger WHERE tgname = 'trg_mem_lessons_rule_cap'"
        )
        # The cap is still enforced for everyone else after the rollback.
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
                " VALUES ('x', $1, 'info', 'input', 'Bash', 'x')", "x" * 281,
            )
    finally:
        await conn.close()
    assert row["rule"] == LONG and row["detail"] is None and row["raw_text"] == "rb-raw"
    assert enabled == "O"


async def test_rollback_rejects_bad_table_name(db, capsys):
    code = await cl.main(["--rollback", "mem_lessons; SELECT 1", "--dsn", db])
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err


async def test_rollback_refused_after_finalize(db, stub, capsys):
    conn = await asyncpg.connect(db)
    try:
        backup = await cl.apply_review(conn, await cl.build_review(conn), embed=fake_embed)
        await cl.validate_constraint(conn)
    finally:
        await conn.close()
    code = await cl.main(["--rollback", backup, "--dsn", db])
    err = capsys.readouterr().err
    assert code == 2
    assert "finalized" in err and "Traceback" not in err


async def test_apply_db_permission_error_is_clean(db, tmp_path, monkeypatch, capsys):
    async def denied(conn, rows, embed=None):
        raise asyncpg.InsufficientPrivilegeError("permission denied for schema public")
    monkeypatch.setattr(cl, "apply_review", denied)
    review = tmp_path / "r.json"
    review.write_text('{"lessons": []}')
    code = await cl.main(["--apply", "--review-file", str(review), "--dsn", db])
    err = capsys.readouterr().err
    assert code == 2
    assert "permission denied" in err and "Traceback" not in err


async def test_rollback_touches_only_rows_this_apply_condensed(throwaway_db, stub, capsys):
    """Codex review: a rollback must not revert unrelated edits made after
    --apply, nor a condensed row someone edited since."""

    conn = await asyncpg.connect(throwaway_db)
    try:
        # Fresh scenario in the same DB: undo finalize so the transition applies.
        await conn.execute("ALTER TABLE mem_lessons DROP CONSTRAINT chk_lesson_rule_len")
        await conn.execute(
            "CREATE TRIGGER trg_mem_lessons_rule_cap BEFORE INSERT OR UPDATE ON mem_lessons"
            " FOR EACH ROW EXECUTE FUNCTION mem_lessons_enforce_rule_cap()"
        )
        await conn.execute("ALTER TABLE mem_lessons DISABLE TRIGGER trg_mem_lessons_rule_cap")
        for title in ("s1", "s2"):
            await conn.execute(
                "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
                " VALUES ($1, $2, 'warning', 'input', 'Bash', $1)", title, LONG,
            )
        await conn.execute(
            "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text)"
            " VALUES ('bystander', 'Short and untouched.', 'info', 'input', 'Bash', 'b')"
        )
        await conn.execute("ALTER TABLE mem_lessons ENABLE TRIGGER trg_mem_lessons_rule_cap")

        rows = [r for r in await cl.build_review(conn) if r["title"] in ("s1", "s2")]
        backup = await cl.apply_review(conn, rows, embed=fake_embed)
        # After the apply: an unrelated lesson and one condensed lesson get edited.
        await conn.execute("UPDATE mem_lessons SET rule = 'Edited later.' WHERE title = 'bystander'")
        await conn.execute("UPDATE mem_lessons SET rule = 'Hand-tuned.' WHERE title = 's2'")
    finally:
        await conn.close()

    code = await cl.main(["--rollback", backup, "--dsn", throwaway_db])
    out = capsys.readouterr()
    assert code == 0, out.err
    conn = await asyncpg.connect(throwaway_db)
    try:
        got = dict(await conn.fetch(
            "SELECT title, rule FROM mem_lessons WHERE title IN ('s1', 's2', 'bystander')"
        ))
    finally:
        await conn.close()
    assert got == {"s1": LONG, "s2": "Hand-tuned.", "bystander": "Edited later."}
    assert "skipped 1" in out.out
