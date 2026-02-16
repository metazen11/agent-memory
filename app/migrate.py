"""
Versioned SQL migration system for agent-memory.

Migrations live in scripts/migrations/ as numbered .sql files:
  001-initial-schema.sql
  002-add-new-column.sql
  ...

A tracking table (mem_schema_migrations) records which have been applied.
Migrations run in order, exactly once, inside a transaction.
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
    version     INTEGER NOT NULL UNIQUE,
    filename    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def ensure_tracking_table(conn: asyncpg.Connection):
    """Create the migration tracking table if it doesn't exist."""
    await conn.execute(TRACKING_TABLE_DDL)


async def get_applied_versions(conn: asyncpg.Connection) -> set[int]:
    """Return set of already-applied migration version numbers."""
    rows = await conn.fetch("SELECT version FROM mem_schema_migrations ORDER BY version")
    return {row["version"] for row in rows}


def discover_migrations() -> list[tuple[int, str, Path]]:
    """
    Scan MIGRATIONS_DIR for files matching NNN-*.sql.
    Returns sorted list of (version, filename, path).
    """
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return []

    pattern = re.compile(r"^(\d{3,})-.*\.sql$")
    migrations = []

    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        match = pattern.match(entry.name)
        if match:
            version = int(match.group(1))
            migrations.append((version, entry.name, entry))

    return sorted(migrations, key=lambda m: m[0])


async def run_migrations(dsn: str, dry_run: bool = False) -> list[str]:
    """
    Connect to the database and run any unapplied migrations.

    Args:
        dsn: Database connection string
        dry_run: If True, list pending migrations without applying them

    Returns list of applied (or would-be-applied) migration filenames.
    """
    # Convert URL format if needed
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)

    conn = await asyncpg.connect(dsn)
    applied_names = []

    try:
        await ensure_tracking_table(conn)
        applied = await get_applied_versions(conn)
        migrations = discover_migrations()

        if not migrations:
            logger.info("No migration files found")
            return applied_names

        pending = [(v, name, path) for v, name, path in migrations if v not in applied]

        if not pending:
            logger.info(f"Database up to date ({len(applied)} migrations applied)")
            return applied_names

        if dry_run:
            logger.info(f"DRY RUN — {len(pending)} migration(s) would be applied:")
            for version, filename, filepath in pending:
                logger.info(f"  Would apply: {filename}")
                applied_names.append(filename)
            logger.info("No changes made to the database")
            return applied_names

        logger.info(f"Running {len(pending)} pending migration(s)...")

        for version, filename, filepath in pending:
            sql = filepath.read_text()
            logger.info(f"  Applying {filename}...")

            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO mem_schema_migrations (version, filename) VALUES ($1, $2)",
                    version, filename,
                )

            applied_names.append(filename)
            logger.info(f"  Applied {filename}")

        logger.info(f"All migrations complete ({len(applied) + len(pending)} total)")
    finally:
        await conn.close()

    return applied_names


async def run_migrations_with_pool(pool: asyncpg.Pool) -> list[str]:
    """
    Run migrations using an existing connection pool.
    Used by FastAPI startup.
    """
    applied_names = []

    async with pool.acquire() as conn:
        await ensure_tracking_table(conn)
        applied = await get_applied_versions(conn)
        migrations = discover_migrations()

        pending = [(v, name, path) for v, name, path in migrations if v not in applied]

        if not pending:
            logger.info(f"Database up to date ({len(applied)} migrations applied)")
            return applied_names

        logger.info(f"Running {len(pending)} pending migration(s)...")

        for version, filename, filepath in pending:
            sql = filepath.read_text()
            logger.info(f"  Applying {filename}...")

            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO mem_schema_migrations (version, filename) VALUES ($1, $2)",
                    version, filename,
                )

            applied_names.append(filename)
            logger.info(f"  Applied {filename}")

    logger.info(f"All migrations complete ({len(applied) + len(pending)} total)")
    return applied_names
