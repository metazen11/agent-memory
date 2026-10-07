#!/usr/bin/env python3
"""One-time, review-gated backfill: condense every lesson whose rule > 280.

Issue #75. Three separate, explicit steps:

1. Dry run (DEFAULT, read-only): condense every lesson whose rule is over
   280 chars, ACTIVE AND INACTIVE, and write a review file (JSON + a
   Markdown rendering). Inactive rows are included: left long they would
   block the final CHECK, and reactivating one would revive a long rule::

       .venv/bin/python scripts/condense_lessons.py --out review.json

2. Apply exactly the reviewed rows (edit/delete entries in the JSON first if
   you disagree with a condensed rule). Copies mem_lessons to
   ``mem_lessons_backup_<UTC timestamp>`` and updates the rows in ONE
   transaction. A row whose rule changed since the review aborts the whole
   apply::

       .venv/bin/python scripts/condense_lessons.py --apply --review-file review.json

3. Finalize migration 019: install the validated
   CHECK (char_length(rule) <= 280) and drop the transition trigger.
   Refused while any rule is still over 280 chars::

       .venv/bin/python scripts/condense_lessons.py --validate-constraint

Rollback of step 2 (only before step 3), using the backup table name
--apply printed::

       .venv/bin/python scripts/condense_lessons.py --rollback mem_lessons_backup_<ts>

It restores rule/detail/raw_text/embedding, in one transaction, for
exactly the rows that --apply condensed and that still hold the rule it
wrote. Unrelated lessons, and condensed lessons edited since, are left
alone (and counted as skipped). After step 3 it refuses: the
finalized CHECK forbids long rules, so run 019's down migration first.

Every mode exits 2 with a one-line ``error:`` message on an expected
failure (stale review, long rows left, missing privilege, bad name).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

ROOT = Path(__file__).absolute().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lesson_condense import (  # noqa: E402
    MAX_RULE_CHARS,
    CondenseRejected,
    lesson_raw_text,
    prepare_rule,
)

CONSTRAINT = "chk_lesson_rule_len"


class ReviewMismatch(RuntimeError):
    """The live row no longer matches what was reviewed."""


async def build_review(conn) -> list[dict]:
    """Condense every over-long rule, active or not. Read-only."""
    rows = await conn.fetch(
        "SELECT id, title, rule, severity, active FROM mem_lessons"
        " WHERE char_length(rule) > $1 ORDER BY id",
        MAX_RULE_CHARS,
    )
    review = []
    for row in rows:
        entry = {
            "id": row["id"],
            "title": row["title"],
            "severity": row["severity"],
            "active": row["active"],
            "old_length": len(row["rule"]),
            "old_rule": row["rule"],
        }
        try:
            prepared = await prepare_rule(row["rule"])
        except CondenseRejected as error:
            entry.update(new_rule=None, new_length=None, provider=None, error=str(error))
        else:
            entry.update(
                new_rule=prepared.rule,
                new_length=len(prepared.rule),
                provider=prepared.provider,
            )
        review.append(entry)
        print(f"#{entry['id']:>5} {entry['old_length']:>5} -> {entry['new_length']}  {entry['title'][:60]}",
              file=sys.stderr)
    return review


def write_review(rows: list[dict], path: Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "max_rule_chars": MAX_RULE_CHARS,
        "count": len(rows),
        "lessons": rows,
    }, indent=2) + "\n")
    md = [f"# Lesson condense review ({len(rows)} lessons)\n"]
    for r in rows:
        md.append(f"## #{r['id']} {r['title']} ({r['severity']})\n")
        md.append(f"**New ({r['new_length']} chars, {r.get('provider')}):** {r['new_rule']}\n")
        if r.get("error"):
            md.append(f"**ERROR:** {r['error']}\n")
        md.append(f"<details><summary>Old ({r['old_length']} chars)</summary>\n\n{r['old_rule']}\n\n</details>\n")
    path.with_suffix(".md").write_text("\n".join(md))
    return path


async def _embed(text: str) -> str | None:
    from app.embeddings import embed_text

    vector = await embed_text(text)
    return "[" + ",".join(str(v) for v in vector) + "]"


async def apply_review(conn, rows: list[dict], embed=_embed) -> str:
    """Back up mem_lessons, then apply exactly ``rows`` in one transaction.

    ``raw_text`` and ``embedding`` are rebuilt from the condensed rule plus
    detail (the same shape as every other writer), so search and dedup see
    the reviewed text. Embeddings are computed BEFORE the transaction opens.
    If they cannot be computed, nothing is applied. Returns the backup
    table name.
    """
    for r in rows:
        new = r.get("new_rule")
        if not new or len(new) > MAX_RULE_CHARS:
            raise ValueError(f"lesson #{r['id']}: reviewed new_rule missing or > {MAX_RULE_CHARS} chars")
    prepared = []
    for r in rows:
        current = await conn.fetchrow("SELECT rule, detail FROM mem_lessons WHERE id = $1", r["id"])
        if current is None or current["rule"] != r["old_rule"]:
            # Checked before any embedding work; re-checked in the UPDATE.
            raise ReviewMismatch(
                f"lesson #{r['id']} changed since review (or is gone); nothing applied"
            )
        current_detail = current["detail"]
        detail = f"{r['old_rule']}\n\n{current_detail}" if current_detail else r["old_rule"]
        raw_text = lesson_raw_text(r["title"], r["new_rule"], detail)
        prepared.append((r, current_detail, detail, raw_text, await embed(raw_text)))
    # All lower case: Postgres folds unquoted identifiers, so the printed
    # name must work when pasted unquoted into psql or --rollback.
    backup = "mem_lessons_backup_" + datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S%f")
    async with conn.transaction():
        await conn.execute(f'CREATE TABLE "{backup}" AS TABLE mem_lessons')
        # Record which rows THIS apply rewrites, and to what, so --rollback
        # can restore exactly those and skip anything edited afterwards.
        await conn.execute(
            f'ALTER TABLE "{backup}" ADD COLUMN condensed_by_apply BOOLEAN NOT NULL DEFAULT false,'
            ' ADD COLUMN applied_rule TEXT'
        )
        await conn.executemany(
            f'UPDATE "{backup}" SET condensed_by_apply = true, applied_rule = $2 WHERE id = $1',
            [(r["id"], r["new_rule"]) for r in rows],
        )
        for r, current_detail, detail, raw_text, embedding in prepared:
            status = await conn.execute(
                """
                UPDATE mem_lessons
                   SET rule = $2, detail = $4, raw_text = $5,
                       embedding = $6::vector
                 WHERE id = $1 AND rule = $3 AND detail IS NOT DISTINCT FROM $7
                """,
                r["id"], r["new_rule"], r["old_rule"], detail, raw_text, embedding,
                current_detail,
            )
            if status != "UPDATE 1":
                raise ReviewMismatch(
                    f"lesson #{r['id']} changed since review (or is gone); nothing applied"
                )
    return backup


async def validate_constraint(conn) -> None:
    """Swap 019's transition trigger for the validated CHECK (see 019).

    Raises asyncpg.CheckViolationError while any rule is over 280 chars."""
    await conn.execute("SELECT mem_lessons_finalize_rule_cap()")


_BACKUP_NAME = re.compile(r"mem_lessons_backup_[0-9a-z_]{1,40}")


class RollbackRefused(RuntimeError):
    pass


async def rollback(conn, backup: str) -> int:
    """Restore rule/detail/raw_text/embedding from a --apply backup table.

    Runs in one transaction. While 019's transition trigger exists, the
    restore must write long rules back, which the trigger refuses, so the
    trigger is disabled for this transaction only. ALTER TABLE ... DISABLE
    TRIGGER is transactional and holds an ACCESS EXCLUSIVE lock until
    commit, so no other session can write while it is off. On any error
    the whole thing rolls back, trigger included.

    Restores only the rows that apply condensed and that still carry the
    rule it wrote. Returns ``(restored, skipped_edited_since)``.
    """
    if not _BACKUP_NAME.fullmatch(backup):
        raise RollbackRefused(f"not a condense_lessons backup table name: {backup!r}")
    if await conn.fetchval("SELECT to_regclass($1)", backup) is None:
        raise RollbackRefused(f"backup table {backup} does not exist")
    if not await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_name = $1 AND column_name = 'condensed_by_apply'", backup,
    ):
        raise RollbackRefused(f"{backup} does not record which rows were condensed; restore by hand")
    if await conn.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname = $1", CONSTRAINT
    ):
        raise RollbackRefused(
            f"019 is finalized ({CONSTRAINT} installed); long rules cannot be restored. "
            "Run 019-lesson-rule-length.down.sql first."
        )
    has_trigger = await conn.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_mem_lessons_rule_cap'"
    )
    async with conn.transaction():
        if has_trigger:
            await conn.execute("ALTER TABLE mem_lessons DISABLE TRIGGER trg_mem_lessons_rule_cap")
        # Only rows this apply condensed AND still holding the rule it wrote.
        # Unrelated lessons, and condensed ones edited since, are untouched.
        restored = await conn.fetchval(f"""
            WITH r AS (
                UPDATE mem_lessons l
                   SET rule = b.rule, detail = b.detail,
                       raw_text = b.raw_text, embedding = b.embedding
                  FROM {backup} b
                 WHERE b.id = l.id AND b.condensed_by_apply AND l.rule = b.applied_rule
                RETURNING l.id
            ) SELECT count(*) FROM r
        """)
        skipped = await conn.fetchval(f"""
            SELECT count(*) FROM {backup} b JOIN mem_lessons l ON l.id = b.id
             WHERE b.condensed_by_apply AND l.rule IS DISTINCT FROM b.rule
               AND l.rule IS DISTINCT FROM b.applied_rule
        """)
        if has_trigger:
            await conn.execute("ALTER TABLE mem_lessons ENABLE TRIGGER trg_mem_lessons_rule_cap")
    return restored, skipped


def _dsn() -> str:
    from app.config import settings
    return settings.effective_database_url


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) write a review file; no DB writes")
    mode.add_argument("--apply", action="store_true", help="apply a reviewed file (requires --review-file)")
    mode.add_argument("--rollback", metavar="BACKUP_TABLE",
                      help="undo --apply from the backup table it printed (before finalize)")
    mode.add_argument("--validate-constraint", action="store_true",
                      help=f"install the validated {CONSTRAINT} CHECK (after a full apply)")
    parser.add_argument("--out", type=Path, help="dry-run output path (default: ./lesson-condense-review-<ts>.json)")
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--dsn", help="override DATABASE_URL")
    args = parser.parse_args(argv)

    conn = await asyncpg.connect(args.dsn or _dsn())
    try:
        if args.apply:
            if not args.review_file:
                parser.error("--apply requires --review-file")
            rows = json.loads(args.review_file.read_text())["lessons"]
            try:
                backup = await apply_review(conn, rows)
            except (ReviewMismatch, ValueError, asyncpg.PostgresError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
            print(f"applied {len(rows)} lessons; backup table: {backup}")
        elif args.rollback:
            try:
                restored, skipped = await rollback(conn, args.rollback)
            except (RollbackRefused, asyncpg.PostgresError) as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
            print(f"restored {restored} lessons from {args.rollback}; skipped {skipped}"
                  " condensed lessons edited since --apply")
        elif args.validate_constraint:
            try:
                await validate_constraint(conn)
            except asyncpg.CheckViolationError:
                remaining = await conn.fetchval(
                    "SELECT count(*) FROM mem_lessons WHERE char_length(rule) > $1",
                    MAX_RULE_CHARS,
                )
                print(f"error: {remaining} lessons still exceed {MAX_RULE_CHARS} chars;"
                      " run the dry run + --apply first. Nothing changed.", file=sys.stderr)
                return 2
            except asyncpg.PostgresError as error:
                print(f"error: {error}", file=sys.stderr)
                return 2
            print(f"{CONSTRAINT} installed and validated; transition trigger dropped")
        else:
            out = args.out or Path(
                f"lesson-condense-review-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
            )
            rows = await build_review(conn)
            path = write_review(rows, out)
            failed = sum(1 for r in rows if r.get("error"))
            print(f"review file: {path} ({len(rows)} lessons, {failed} not condensable)")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
