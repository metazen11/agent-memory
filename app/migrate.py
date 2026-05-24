"""
Versioned SQL migration system for agent-memory.

Migrations live in scripts/migrations/ as numbered .sql files:
  001-initial-schema.sql
  002-add-new-column.sql
  ...

A tracking table (mem_schema_migrations) records which have been applied.
Migrations run in order, exactly once, inside a transaction.

Companion ``.concurrent.sql`` files
-----------------------------------
A migration may ship a companion ``NNN-name.concurrent.sql`` file containing
statements that CANNOT run inside a transaction (notably
``CREATE INDEX CONCURRENTLY`` and ``ALTER TABLE ... VALIDATE CONSTRAINT`` when
following a ``NOT VALID`` FK).

The runner picks these companions up alongside their base migration:

* Each file gets its own row in ``mem_schema_migrations`` (one tracking
  row per file, same ``version``-derived prefix).
* Their statements run OUTSIDE any wrapping transaction (asyncpg wraps a
  multi-statement ``execute()`` in an implicit BEGIN/COMMIT, which is
  incompatible with ``CREATE INDEX CONCURRENTLY``). The file body is split
  into individual statements and each is issued separately.
* They run AFTER their base transactional file in the same startup pass.

See ``docs/fine_tune/V2_DATA_PIPELINE_PLAN.md`` Step 1 for the motivating
use case (migration 012).
"""

import asyncio
import logging
import os
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "scripts" / "migrations"

TRACKING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS mem_schema_migrations (
    id          SERIAL PRIMARY KEY,
    version     INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Match e.g. ``012-v2-data-pipeline.sql`` or ``012-v2-data-pipeline.concurrent.sql``.
_MIGRATION_FILE_RE = re.compile(r"^(\d{3,})-.*\.sql$")
_CONCURRENT_SUFFIX = ".concurrent.sql"
_DOWN_SUFFIX = ".down.sql"


async def ensure_tracking_table(conn: asyncpg.Connection):
    """Create the migration tracking table if it doesn't exist.

    Also performs a one-time, idempotent migration of legacy schemas where
    ``mem_schema_migrations`` carried ``UNIQUE(version)`` (one row per
    migration version). The new pattern lets a base transactional file and
    its ``.concurrent.sql`` companion both record themselves under the same
    version, so the constraint is moved from ``version`` to ``filename``.

    All DDL here is idempotent and safe to run on every startup.
    """
    await conn.execute(TRACKING_TABLE_DDL)

    # Detect legacy UNIQUE(version) constraint and drop it.
    legacy_version_unique = await conn.fetchval(
        """
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'mem_schema_migrations'
          AND c.contype = 'u'
          AND (SELECT array_agg(attname ORDER BY attnum)
               FROM pg_attribute
               WHERE attrelid = t.oid AND attnum = ANY(c.conkey))
              = ARRAY['version']::name[]
        LIMIT 1
        """
    )
    if legacy_version_unique:
        await conn.execute(
            f'ALTER TABLE mem_schema_migrations DROP CONSTRAINT "{legacy_version_unique}"'
        )

    # Ensure UNIQUE(filename) is present.
    filename_unique_exists = await conn.fetchval(
        """
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'mem_schema_migrations'
          AND c.contype = 'u'
          AND (SELECT array_agg(attname ORDER BY attnum)
               FROM pg_attribute
               WHERE attrelid = t.oid AND attnum = ANY(c.conkey))
              = ARRAY['filename']::name[]
        LIMIT 1
        """
    )
    if not filename_unique_exists:
        await conn.execute(
            "ALTER TABLE mem_schema_migrations "
            "ADD CONSTRAINT mem_schema_migrations_filename_key UNIQUE (filename)"
        )


async def get_applied_filenames(conn: asyncpg.Connection) -> set[str]:
    """Return set of already-applied migration filenames."""
    rows = await conn.fetch(
        "SELECT filename FROM mem_schema_migrations ORDER BY version, id"
    )
    return {row["filename"] for row in rows}


def _classify(entry: Path) -> tuple[int, str, bool] | None:
    """Return (version, filename, is_concurrent) or None if not a migration file.

    ``.down.sql`` reverse migrations are intentionally skipped — they are
    meant to be applied manually for rollback.
    """
    name = entry.name
    if name.endswith(_DOWN_SUFFIX):
        return None
    match = _MIGRATION_FILE_RE.match(name)
    if not match:
        return None
    version = int(match.group(1))
    return version, name, name.endswith(_CONCURRENT_SUFFIX)


def discover_migrations() -> list[tuple[int, str, Path, bool]]:
    """
    Scan MIGRATIONS_DIR for files matching NNN-*.sql.

    Returns sorted list of ``(version, filename, path, is_concurrent)`` tuples.
    Sort key is ``(version, is_concurrent)`` so a base transactional file
    always runs before its ``.concurrent.sql`` companion at the same version.
    """
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return []

    migrations: list[tuple[int, str, Path, bool]] = []
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        classified = _classify(entry)
        if classified is None:
            continue
        version, filename, is_concurrent = classified
        migrations.append((version, filename, entry, is_concurrent))

    return sorted(migrations, key=lambda m: (m[0], m[3]))


def _split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into top-level statements on ``;`` boundaries.

    Used only for ``.concurrent.sql`` files: asyncpg wraps a multi-statement
    ``execute()`` in an implicit BEGIN/COMMIT, which Postgres rejects when
    the script contains ``CREATE INDEX CONCURRENTLY``. By issuing each
    statement as a separate ``execute()`` we stay in auto-commit mode.

    The splitter understands single-line ``--`` comments, ``/* ... */``
    block comments, ``'...'`` string literals (including ``''`` escapes),
    and ``$tag$ ... $tag$`` dollar-quoted bodies. Concurrent migration
    files typically contain only plain DDL, but those forms are recognised
    defensively to avoid future surprises.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_line_comment = False
    in_block_comment = False
    in_single_quote = False
    dollar_tag: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if dollar_tag is not None:
            close = f"${dollar_tag}$"
            if sql.startswith(close, i):
                buf.append(close)
                i += len(close)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if in_single_quote:
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_single_quote = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "'":
            in_single_quote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < n and sql[j] == "$":
                dollar_tag = sql[i + 1:j]
                buf.append(sql[i:j + 1])
                i = j + 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


async def _apply_one(
    conn: asyncpg.Connection,
    version: int,
    filename: str,
    filepath: Path,
    is_concurrent: bool,
) -> None:
    """Apply a single migration file and record it in the tracking table.

    Transactional files run inside ``conn.transaction()``. Concurrent files
    are split into individual statements and each is issued under
    auto-commit; the tracking-table INSERT then runs as its own auto-
    committed statement.
    """
    sql = filepath.read_text()
    logger.info(f"  Applying {filename}{' (concurrent)' if is_concurrent else ''}...")

    if is_concurrent:
        for stmt in _split_sql_statements(sql):
            await conn.execute(stmt)
        await conn.execute(
            "INSERT INTO mem_schema_migrations (version, filename) VALUES ($1, $2) "
            "ON CONFLICT (filename) DO NOTHING",
            version, filename,
        )
    else:
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO mem_schema_migrations (version, filename) "
                "VALUES ($1, $2) ON CONFLICT (filename) DO NOTHING",
                version, filename,
            )

    logger.info(f"  Applied {filename}")


async def run_migrations(dsn: str, dry_run: bool = False) -> list[str]:
    """
    Connect to the database and run any unapplied migrations.

    Args:
        dsn: Database connection string
        dry_run: If True, list pending migrations without applying them

    Returns list of applied (or would-be-applied) migration filenames.
    """
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)

    conn = await asyncpg.connect(dsn)
    applied_names: list[str] = []

    try:
        await ensure_tracking_table(conn)
        applied = await get_applied_filenames(conn)
        migrations = discover_migrations()

        if not migrations:
            logger.info("No migration files found")
            return applied_names

        pending = [m for m in migrations if m[1] not in applied]

        if not pending:
            logger.info(f"Database up to date ({len(applied)} migrations applied)")
            return applied_names

        if dry_run:
            logger.info(f"DRY RUN — {len(pending)} migration(s) would be applied:")
            for _, filename, _, is_concurrent in pending:
                tag = " (concurrent)" if is_concurrent else ""
                logger.info(f"  Would apply: {filename}{tag}")
                applied_names.append(filename)
            logger.info("No changes made to the database")
            return applied_names

        logger.info(f"Running {len(pending)} pending migration(s)...")

        for version, filename, filepath, is_concurrent in pending:
            await _apply_one(conn, version, filename, filepath, is_concurrent)
            applied_names.append(filename)

        logger.info(f"All migrations complete ({len(applied) + len(pending)} total)")
    finally:
        await conn.close()

    return applied_names


async def run_migrations_with_pool(pool: asyncpg.Pool) -> list[str]:
    """
    Run migrations using an existing connection pool.
    Used by FastAPI startup.
    """
    applied_names: list[str] = []

    async with pool.acquire() as conn:
        await ensure_tracking_table(conn)
        applied = await get_applied_filenames(conn)
        migrations = discover_migrations()

        pending = [m for m in migrations if m[1] not in applied]

        if not pending:
            logger.info(f"Database up to date ({len(applied)} migrations applied)")
            return applied_names

        logger.info(f"Running {len(pending)} pending migration(s)...")

        for version, filename, filepath, is_concurrent in pending:
            await _apply_one(conn, version, filename, filepath, is_concurrent)
            applied_names.append(filename)

    logger.info(f"All migrations complete ({len(applied) + len(pending)} total)")
    return applied_names
