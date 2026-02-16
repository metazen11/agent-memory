#!/usr/bin/env python3
"""
CLI entry point for running migrations.
Called by install.js and ensure-services.js.

Usage:
    python scripts/run_migrations.py                    # Run pending migrations
    python scripts/run_migrations.py --dry-run          # Show what would run (no changes)
    python scripts/run_migrations.py --backup           # Backup mem_* tables before migrating
    python scripts/run_migrations.py --backup-only      # Just backup, don't migrate
    python scripts/run_migrations.py --dsn postgresql://user:pass@host:port/db

Reads DATABASE_URL from .env if --dsn not provided.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path so we can import app modules
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_dsn() -> str:
    """Build DSN from args, env vars, or .env file."""
    # Check --dsn argument
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--dsn" and i < len(sys.argv) - 1:
            return sys.argv[i + 1]
        if arg.startswith("--dsn="):
            return arg.split("=", 1)[1]

    # Try loading .env
    env_file = project_root / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val:
                os.environ.setdefault(key, val)

    # Check for DATABASE_URL override
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("AGENT_MEMORY_DATABASE_URL")
    if database_url:
        return database_url

    # Build from components
    user = os.environ.get("POSTGRES_USER", "agentmem")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5433")
    db = os.environ.get("POSTGRES_DB", "agent_memory")

    pw = f":{password}" if password else ""
    return f"postgresql://{user}{pw}@{host}:{port}/{db}"


async def backup_database(dsn: str) -> str:
    """
    Create a backup of all mem_* tables into timestamped backup tables.
    Returns the backup suffix used (e.g., '_bak_20260215_2100').
    """
    import asyncpg
    import re

    suffix = datetime.now().strftime("_bak_%Y%m%d_%H%M")

    conn_dsn = dsn
    if conn_dsn.startswith("postgresql://"):
        conn_dsn = conn_dsn.replace("postgresql://", "postgres://", 1)

    conn = await asyncpg.connect(conn_dsn)
    try:
        # Find all mem_* tables (excluding backups and the migration tracking table)
        rows = await conn.fetch("""
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public'
              AND tablename LIKE 'mem\\_%'
              AND tablename NOT LIKE 'mem\\_%\\_bak\\_%'
              AND tablename != 'mem_schema_migrations'
            ORDER BY tablename
        """)

        tables = [row["tablename"] for row in rows]

        if not tables:
            logger.info("No mem_* tables found to backup")
            return suffix

        logger.info(f"Backing up {len(tables)} table(s) with suffix '{suffix}'...")

        for table in tables:
            backup_table = f"{table}{suffix}"
            # Drop old backup with same name if exists
            await conn.execute(f"DROP TABLE IF EXISTS {backup_table} CASCADE")
            # CREATE TABLE AS preserves data and column types
            await conn.execute(f"CREATE TABLE {backup_table} AS TABLE {table}")
            count = await conn.fetchval(f"SELECT count(*) FROM {backup_table}")
            logger.info(f"  {table} → {backup_table} ({count} rows)")

        logger.info(f"Backup complete. Restore with: ALTER TABLE mem_xxx{suffix} RENAME TO mem_xxx")
    finally:
        await conn.close()

    return suffix


async def main():
    from app.migrate import run_migrations

    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args
    do_backup = "--backup" in args or "--backup-only" in args
    backup_only = "--backup-only" in args

    dsn = get_dsn()

    # Mask password in log output
    import re
    safe_dsn = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
    logger.info(f"Connecting to {safe_dsn}")

    try:
        if do_backup:
            suffix = await backup_database(dsn)
            print(f"Backup created with suffix: {suffix}")

        if backup_only:
            print("Backup-only mode — skipping migrations")
            return

        if dry_run:
            print("DRY RUN — no changes will be made")

        applied = await run_migrations(dsn, dry_run=dry_run)
        if applied:
            verb = "Would apply" if dry_run else "Applied"
            print(f"{verb} {len(applied)} migration(s): {', '.join(applied)}")
        else:
            print("Database schema is up to date")
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
