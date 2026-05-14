#!/usr/bin/env python3
"""One-time consolidation of mem_projects rows post-migration-013 (issue #36).

Walks every existing ``mem_projects`` row, resolves its ``full_path`` to a
git context, and:

* Sets ``canonical_root_path``, ``git_remote``, and ``source_kind`` on rows
  that didn't have them (i.e. created before migration 013).
* For sub-folder rows whose canonical_root_path points at a DIFFERENT
  existing row's full_path, sets ``parent_project_id`` so reads can collapse.
* Re-maps existing ``mem_tool_calls.project_id`` so calls originally tagged
  with a sub-folder project end up under the canonical row instead.
* Does NOT DELETE any duplicate row — preserves referential integrity for
  observations, sessions, lessons, prompts, and other foreign keys that
  point at the old row.

Dry-run is the default. ``--commit`` performs the writes inside a single
transaction.

Usage::

    .venv/bin/python scripts/backfill/consolidate_projects.py             # dry-run
    .venv/bin/python scripts/backfill/consolidate_projects.py --commit    # apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Allow `from app...` when invoked as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncpg

from app.config import settings
from app.git_context import resolve_git_context

logger = logging.getLogger(__name__)


# ── SQL ───────────────────────────────────────────────

_SELECT_ALL_PROJECTS_SQL = (
    "SELECT id, full_path, canonical_root_path, git_remote, source_kind "
    "FROM mem_projects ORDER BY id"
)

_SELECT_PROJECT_BY_PATH_SQL = (
    "SELECT id FROM mem_projects WHERE full_path = $1"
)

_COUNT_TOOL_CALLS_BY_PROJECT_SQL = (
    "SELECT count(*) FROM mem_tool_calls WHERE project_id = $1"
)

_UPDATE_PROJECT_FIELDS_SQL = """
UPDATE mem_projects
SET canonical_root_path = COALESCE($2, canonical_root_path),
    git_remote          = COALESCE($3, git_remote),
    source_kind         = COALESCE($4, source_kind),
    parent_project_id   = COALESCE($5, parent_project_id)
WHERE id = $1
"""

_REMAP_TOOL_CALLS_SQL = (
    "UPDATE mem_tool_calls SET project_id = $1 WHERE project_id = $2"
)

_POST_APPLY_RATIO_SQL = """
SELECT
    count(*) FILTER (WHERE source_kind = 'git')             AS git_rows,
    count(*) FILTER (WHERE source_kind = 'non-git')         AS non_git_rows,
    count(*) FILTER (WHERE source_kind = 'ephemeral')       AS ephemeral_rows,
    count(*) FILTER (WHERE canonical_root_path IS NOT NULL) AS with_canonical,
    count(*)                                                AS total
FROM mem_projects
"""


async def _plan_row(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
) -> dict[str, Any] | None:
    """Decide the canonical-row + parent + remap actions for one mem_projects row.

    Returns a plan dict, or None if no change needed.
    """
    pid = row["id"]
    full_path = row["full_path"]
    existing_canonical = row["canonical_root_path"]
    existing_remote = row["git_remote"]
    existing_kind = row["source_kind"]

    ctx = resolve_git_context(full_path)

    plan: dict[str, Any] = {
        "id": pid,
        "full_path": full_path,
        "new_canonical": None,
        "new_remote": None,
        "new_kind": None,
        "parent_id": None,
        "remap_to_id": None,
        "tool_calls_to_remap": 0,
    }

    # Decide field-level updates (only set if currently NULL).
    if existing_canonical is None and ctx.canonical_root_path:
        plan["new_canonical"] = ctx.canonical_root_path
    if existing_remote is None and ctx.git_remote:
        plan["new_remote"] = ctx.git_remote
    if existing_kind is None or existing_kind == "git":
        # Refresh kind only if it conflicts with current detection.
        if ctx.source_kind != (existing_kind or "git"):
            plan["new_kind"] = ctx.source_kind

    # Find the canonical row this leaf should point at, if any.
    canonical_path = ctx.canonical_root_path
    if (
        ctx.source_kind == "git"
        and canonical_path
        and canonical_path != full_path
    ):
        # This row is a sub-folder of a different (canonical) project.
        canonical_row = await conn.fetchrow(
            _SELECT_PROJECT_BY_PATH_SQL,
            canonical_path,
        )
        if canonical_row:
            plan["parent_id"] = canonical_row["id"]
            plan["remap_to_id"] = canonical_row["id"]

            # Count tool_calls that would be remapped, for the dry-run report.
            count = await conn.fetchval(
                _COUNT_TOOL_CALLS_BY_PROJECT_SQL,
                pid,
            )
            plan["tool_calls_to_remap"] = int(count or 0)

    # No change needed?
    if (
        plan["new_canonical"] is None
        and plan["new_remote"] is None
        and plan["new_kind"] is None
        and plan["parent_id"] is None
        and plan["remap_to_id"] is None
    ):
        return None
    return plan


async def _apply_plan(conn: asyncpg.Connection, plan: dict[str, Any]) -> None:
    """Apply one row's plan. Caller wraps in a transaction."""
    pid = plan["id"]

    # Field updates on the row itself.
    if (
        plan["new_canonical"] is not None
        or plan["new_remote"] is not None
        or plan["new_kind"] is not None
        or plan["parent_id"] is not None
    ):
        await conn.execute(
            _UPDATE_PROJECT_FIELDS_SQL,
            pid,
            plan["new_canonical"],
            plan["new_remote"],
            plan["new_kind"],
            plan["parent_id"],
        )

    # Remap tool_calls if this leaf has a canonical parent and tool_calls
    # to move.
    if plan["remap_to_id"] is not None and plan["tool_calls_to_remap"] > 0:
        await conn.execute(
            _REMAP_TOOL_CALLS_SQL,
            plan["remap_to_id"], pid,
        )


def _summarize(plans: list[dict[str, Any]]) -> None:
    """Print a per-project plan summary table."""
    if not plans:
        logger.info("No changes needed — all mem_projects rows are already consolidated.")
        return

    counts = Counter()
    for p in plans:
        if p["new_canonical"]:
            counts["set_canonical_root_path"] += 1
        if p["new_remote"]:
            counts["set_git_remote"] += 1
        if p["new_kind"]:
            counts["set_source_kind"] += 1
        if p["parent_id"]:
            counts["set_parent_project_id"] += 1
        if p["remap_to_id"]:
            counts["leaf_rows_pointing_at_canonical"] += 1

    total_remap = sum(p["tool_calls_to_remap"] for p in plans)

    logger.info("=" * 72)
    logger.info("Consolidation plan summary")
    logger.info("=" * 72)
    logger.info(f"  Rows with field updates:")
    for k, v in counts.items():
        logger.info(f"    {k:40s} {v:6d}")
    logger.info(f"  tool_calls to remap to canonical: {total_remap}")
    logger.info(f"  Total mem_projects rows touched:  {len(plans)}")
    logger.info("=" * 72)

    # Detail for the first 15 leaf-rows so the operator can spot-check.
    leaves = [p for p in plans if p["remap_to_id"]]
    if leaves:
        logger.info("Sample leaf→canonical remaps (first 15):")
        for p in leaves[:15]:
            logger.info(
                f"  id={p['id']:4d}  {p['full_path'][:60]:<60}"
                f"  → parent_id={p['parent_id']}  "
                f"(tool_calls={p['tool_calls_to_remap']})"
            )


async def consolidate(dsn: str, commit: bool) -> int:
    """Resolve every mem_projects row to its canonical context. Returns exit code."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_SELECT_ALL_PROJECTS_SQL)
        logger.info(f"Inspecting {len(rows)} mem_projects rows...")

        plans: list[dict[str, Any]] = []
        for row in rows:
            plan = await _plan_row(conn, row)
            if plan:
                plans.append(plan)

        _summarize(plans)

        if not commit:
            logger.info("DRY RUN — no changes applied. Re-run with --commit to apply.")
            return 0

        # Apply inside one transaction so the operation is atomic.
        async with conn.transaction():
            for plan in plans:
                await _apply_plan(conn, plan)
        logger.info(f"Applied {len(plans)} consolidation updates.")

        # Post-apply sanity counts.
        ratio = await conn.fetchrow(_POST_APPLY_RATIO_SQL)
        logger.info(
            f"Post-apply: total={ratio['total']} git={ratio['git_rows']} "
            f"non-git={ratio['non_git_rows']} ephemeral={ratio['ephemeral_rows']} "
            f"with_canonical={ratio['with_canonical']}"
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="Apply changes (default is dry-run)")
    p.add_argument("--dsn", default=None,
                   help="Postgres DSN; defaults to app.config.settings")
    args = p.parse_args()

    dsn = args.dsn or settings.database_url
    return asyncio.run(consolidate(dsn, args.commit))


if __name__ == "__main__":
    sys.exit(main())
