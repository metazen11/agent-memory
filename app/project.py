"""Shared project helpers for path-based project scoping.

After migration 013 (issue #36), projects are canonically identified by the
git root of their cwd, with `git_remote` as a secondary dedupe key for
cross-checkout grouping (Dropbox vs local, second machine, worktrees of the
same remote).

Sub-folder cwds (e.g. ``/repo/src/lib``) resolve to the same canonical
project row as their repo root, instead of creating their own row. Non-git
cwds get a row at the literal path. Ephemeral paths (pytest tmp, mktemp)
get a row marked ``source_kind='ephemeral'`` and can be excluded from
training-data export.

Read-path compatibility
-----------------------
``project_path_filter`` is unchanged — it does bidirectional prefix
matching, so a query for ``/repo/src`` still finds the canonical row at
``/repo`` because ``/repo/src LIKE /repo || '/%'``. This keeps the read
contract stable for Claude, Anvil, Codex, and any other client that
passes a sub-folder cwd.

SQL hygiene
-----------
All queries are parametrized via asyncpg ``$1, $2, …`` placeholders. SQL
strings live as module-level constants so they're easy to find and audit;
the codebase doesn't have a repo/ORM layer yet, so this is the convention
followed by the rest of ``app/routes/``.
"""

from pathlib import Path

from app.git_context import resolve_git_context


# ── SQL ───────────────────────────────────────────────

_INSERT_GIT_PROJECT_SQL = """
INSERT INTO mem_projects
    (name, full_path, canonical_root_path, git_remote, source_kind)
VALUES ($1, $2, $2, $3, 'git')
ON CONFLICT (full_path) DO UPDATE SET
    canonical_root_path = COALESCE(mem_projects.canonical_root_path, EXCLUDED.canonical_root_path),
    git_remote          = COALESCE(mem_projects.git_remote, EXCLUDED.git_remote),
    source_kind         = COALESCE(mem_projects.source_kind, 'git')
RETURNING id
"""

_INSERT_NONGIT_OR_EPHEMERAL_PROJECT_SQL = """
INSERT INTO mem_projects (name, full_path, source_kind)
VALUES ($1, $2, $3)
ON CONFLICT (full_path) DO UPDATE SET
    source_kind = COALESCE(mem_projects.source_kind, EXCLUDED.source_kind)
RETURNING id
"""


# ── Helpers ───────────────────────────────────────────

async def ensure_project(conn, full_path: str) -> int:
    """Get or create a project. Returns the canonical project id.

    Behaviour:

    * If ``full_path`` is inside a git repo, the project row is keyed on the
      git toplevel. Sub-folder cwds collapse to the same row. ``git_remote``,
      ``canonical_root_path``, and ``source_kind='git'`` are populated from
      the resolved git context.
    * If ``full_path`` is a real cwd not in a git repo, a row is created at
      the literal path with ``source_kind='non-git'``.
    * If ``full_path`` is an ephemeral test/scratch path (pytest tmp,
      mktemp), a row is created with ``source_kind='ephemeral'``. These are
      filtered out of training-data export.

    Existing rows from before migration 013 are upgraded in place on first
    use: ``canonical_root_path``, ``git_remote``, and ``source_kind`` get
    filled in via ``COALESCE`` on conflict.
    """
    ctx = resolve_git_context(full_path)

    if ctx.source_kind == "git" and ctx.canonical_root_path:
        canonical_path = ctx.canonical_root_path
        name = Path(canonical_path).name or canonical_path
        row = await conn.fetchrow(
            _INSERT_GIT_PROJECT_SQL,
            name, canonical_path, ctx.git_remote,
        )
        return row["id"]

    # Non-git or ephemeral: keep the literal cwd as the row key so unrelated
    # tmp dirs and standalone paths don't collapse into each other.
    name = Path(full_path).name or full_path
    row = await conn.fetchrow(
        _INSERT_NONGIT_OR_EPHEMERAL_PROJECT_SQL,
        name, full_path, ctx.source_kind,
    )
    return row["id"]


def project_path_filter(param_idx: int) -> tuple[str, int]:
    """Return SQL clause + next param index for bidirectional path matching.

    The same parameter value is bound 3 times (as $N, $N+1, $N+2) so each
    reference gets its own placeholder.

    Usage::

        clause, next_idx = project_path_filter(pidx)
        # clause = "(p.full_path = $3 OR p.full_path LIKE $4 || '/%' OR $5 LIKE p.full_path || '/%')"
        # next_idx = 6
        params.extend([project, project, project])  # bind 3 times

    Returns:
        (sql_clause, next_param_idx)
    """
    clause = (
        f"(p.full_path = ${param_idx}"
        f" OR p.full_path LIKE ${param_idx + 1} || '/%'"
        f" OR ${param_idx + 2} LIKE p.full_path || '/%')"
    )
    return clause, param_idx + 3
