"""Shared project helpers for path-based project scoping.

Projects are identified by full_path (the cwd). Queries use bidirectional
prefix matching so a parent directory finds child project memories and vice versa.
"""

from pathlib import Path


async def ensure_project(conn, full_path: str) -> int:
    """Get or create a project by full_path. Returns project id.

    The `name` column is derived from the basename of the path for display.
    """
    row = await conn.fetchrow(
        "SELECT id FROM mem_projects WHERE full_path = $1", full_path
    )
    if row:
        return row["id"]
    name = Path(full_path).name or full_path
    row = await conn.fetchrow(
        "INSERT INTO mem_projects (name, full_path) VALUES ($1, $2) "
        "ON CONFLICT (full_path) DO UPDATE SET name = EXCLUDED.name "
        "RETURNING id",
        name, full_path,
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
