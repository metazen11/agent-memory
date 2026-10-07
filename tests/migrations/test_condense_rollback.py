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


async def test_rollback_touches_only_rows_this_apply_condensed(db, stub, capsys):
    """Codex review: a rollback must not revert unrelated edits made after
    --apply, nor a condensed row someone edited since."""

    conn = await asyncpg.connect(db)
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

    code = await cl.main(["--rollback", backup, "--dsn", db])
    out = capsys.readouterr()
    assert code == 0, out.err
    conn = await asyncpg.connect(db)
    try:
        got = dict(await conn.fetch(
            "SELECT title, rule FROM mem_lessons WHERE title IN ('s1', 's2', 'bystander')"
        ))
    finally:
        await conn.close()
    assert got == {"s1": LONG, "s2": "Hand-tuned.", "bystander": "Edited later."}
    assert "skipped 1" in out.out


async def _transition_with(conn, rows_spec):
    """Put the shared DB back into 019's transition state and seed rows that
    pre-date the cap. rows_spec: [(title, rule, active, severity)]."""
    if await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE conname = 'chk_lesson_rule_len'"):
        await conn.execute("ALTER TABLE mem_lessons DROP CONSTRAINT chk_lesson_rule_len")
    if not await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_mem_lessons_rule_cap'"):
        await conn.execute(
            "CREATE TRIGGER trg_mem_lessons_rule_cap BEFORE INSERT OR UPDATE ON mem_lessons"
            " FOR EACH ROW EXECUTE FUNCTION mem_lessons_enforce_rule_cap()"
        )
    await conn.execute("ALTER TABLE mem_lessons DISABLE TRIGGER trg_mem_lessons_rule_cap")
    for title, rule, active, severity in rows_spec:
        await conn.execute(
            "INSERT INTO mem_lessons (title, rule, severity, trigger_on, trigger_tool, raw_text, active)"
            " VALUES ($1, $2, $4, 'input', 'Bash', $1, $3)", title, rule, active, severity,
        )
    await conn.execute("ALTER TABLE mem_lessons ENABLE TRIGGER trg_mem_lessons_rule_cap")


async def test_rollback_skips_row_reactivated_after_apply(db, stub, capsys):
    """Third audit repro (row 6): inactive critical long row, condensed,
    then legally reactivated (its rule was short). Restoring its long rule
    onto an active critical row would create a state the trigger forbids."""
    conn = await asyncpg.connect(db)
    try:
        await _transition_with(conn, [("react", LONG, False, "critical")])
        rows = [r for r in await cl.build_review(conn) if r["title"] == "react"]
        backup = await cl.apply_review(conn, rows, embed=fake_embed)
        await conn.execute("UPDATE mem_lessons SET active = true WHERE title = 'react'")
    finally:
        await conn.close()
    code = await cl.main(["--rollback", backup, "--dsn", db])
    out = capsys.readouterr()
    assert code == 0, out.err
    conn = await asyncpg.connect(db)
    try:
        row = await conn.fetchrow("SELECT id, rule, active FROM mem_lessons WHERE title = 'react'")
    finally:
        await conn.close()
    assert row["active"] is True and row["rule"] == "Do X.", "long rule restored onto an active row"
    assert f"skipped ids: [{row['id']}]" in out.out


async def test_rollback_skips_row_whose_detail_was_edited(db, stub, capsys):
    """Third audit repro (row 7): detail edited after apply was reset."""
    conn = await asyncpg.connect(db)
    try:
        await _transition_with(conn, [("detail-edit", LONG, True, "warning")])
        rows = [r for r in await cl.build_review(conn) if r["title"] == "detail-edit"]
        backup = await cl.apply_review(conn, rows, embed=fake_embed)
        await conn.execute("UPDATE mem_lessons SET detail = 'Operator note.' WHERE title = 'detail-edit'")
    finally:
        await conn.close()
    code = await cl.main(["--rollback", backup, "--dsn", db])
    out = capsys.readouterr()
    assert code == 0, out.err
    conn = await asyncpg.connect(db)
    try:
        row = await conn.fetchrow("SELECT id, rule, detail FROM mem_lessons WHERE title = 'detail-edit'")
    finally:
        await conn.close()
    assert row["detail"] == "Operator note." and row["rule"] == "Do X."
    assert f"skipped ids: [{row['id']}]" in out.out


async def test_dropped_connection_exits_cleanly(db, monkeypatch, capsys):
    async def dropped(conn, backup):
        raise asyncpg.InterfaceError("connection is closed")
    monkeypatch.setattr(cl, "rollback", dropped)
    code = await cl.main(["--rollback", "mem_lessons_backup_x", "--dsn", db])
    err = capsys.readouterr().err
    assert code == 2
    assert "connection is closed" in err and "Traceback" not in err


async def test_lookups_are_schema_qualified_not_search_path(db, stub, capsys):
    """A same-named table earlier on search_path must not be consulted."""
    conn = await asyncpg.connect(db)
    try:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS decoy")
        await conn.execute("CREATE TABLE decoy.mem_lessons_backup_decoy (id int)")
    finally:
        await conn.close()
    code = await cl.main([
        "--rollback", "mem_lessons_backup_decoy",
        "--dsn", db + ("&" if "?" in db else "?") + "options=-csearch_path%3Ddecoy,public",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "does not exist" in err


async def test_apply_uses_live_title_for_search_text(db, stub):
    """Codex P3: a title edited between review and --apply must not leave
    raw_text/embedding built from the stale reviewed title."""
    conn = await asyncpg.connect(db)
    try:
        await _transition_with(conn, [("old-title", LONG, True, "info")])
        rows = [r for r in await cl.build_review(conn) if r["title"] == "old-title"]
        await conn.execute("UPDATE mem_lessons SET title = 'new-title' WHERE title = 'old-title'")
        await cl.apply_review(conn, rows, embed=fake_embed)
        raw = await conn.fetchval("SELECT raw_text FROM mem_lessons WHERE title = 'new-title'")
    finally:
        await conn.close()
    assert raw.startswith("new-title\n")
