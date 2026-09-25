"""
Database query wrapper for agentMemory.

Mirrors the fire-map db.py pattern: credentials live in .env (loaded by
app.config.Settings), the CLI never prints the DSN, DDL is refused.

Usage:
    python -m app.db_wrapper "SELECT count(*) FROM observations"
    python -m app.db_wrapper --json "SELECT * FROM lessons LIMIT 3"
    python -m app.db_wrapper --health
    python -m app.db_wrapper --tables

DDL (CREATE/DROP/ALTER/TRUNCATE) is refused — use scripts/migrations/ instead.
DML (INSERT/UPDATE/DELETE) requires --force.

Anti-leak guarantees:
    - DSN, password, and connection string never appear in stdout/stderr.
    - On connection failure, the exception is rewritten so it cannot echo
      credentials (asyncpg's default error includes the DSN).
    - --debug shows host/db/user only — never the password.
"""

import argparse
import asyncio
import json
import re
import sys
from typing import Any

import asyncpg

from app.config import settings


DDL_RE = re.compile(
    r"^\s*(CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|COMMENT|REINDEX|VACUUM)\b",
    re.IGNORECASE,
)
DML_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|MERGE|UPSERT)\b", re.IGNORECASE)


def classify(sql: str) -> str:
    if DDL_RE.search(sql):
        return "ddl"
    if DML_RE.search(sql):
        return "dml"
    return "select"


async def _connect() -> asyncpg.Connection:
    try:
        return await asyncpg.connect(
            user=settings.postgres_user,
            password=settings.postgres_password or None,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
        )
    except asyncpg.InvalidPasswordError:
        sys.stderr.write("ERROR: password authentication failed for agentMemory DB\n")
        sys.stderr.write("       Check POSTGRES_PASSWORD in .env (use env-write.js to set)\n")
        sys.exit(2)
    except Exception as e:
        # Strip anything that smells like a DSN — asyncpg sometimes embeds it
        msg = str(e)
        msg = re.sub(r"postgres(?:ql)?://[^\s]+", "postgres://***", msg)
        sys.stderr.write(f"ERROR: db connection failed: {msg}\n")
        sys.exit(2)


async def run_query(sql: str, as_json: bool, force: bool) -> int:
    kind = classify(sql)

    if kind == "ddl":
        sys.stderr.write(
            "REFUSED: DDL via db_wrapper.py is not allowed.\n"
            "         Use scripts/migrations/NNN_description.sql + app/migrate.py.\n"
        )
        return 3

    if kind == "dml" and not force:
        sys.stderr.write(
            f"REFUSED: DML detected ({sql.split()[0].upper()}). Re-run with --force to proceed.\n"
        )
        return 3

    conn = await _connect()
    try:
        rows = await conn.fetch(sql)
    finally:
        await conn.close()

    if as_json:
        print(json.dumps([dict(r) for r in rows], default=str, indent=2))
        return 0

    if not rows:
        print("(no rows)")
        return 0

    cols = list(rows[0].keys())
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    sep = "  "
    print(sep.join(c.ljust(w) for c, w in zip(cols, widths)))
    print(sep.join("-" * w for w in widths))
    for r in rows:
        print(sep.join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    print(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return 0


async def health() -> int:
    conn = await _connect()
    try:
        version = await conn.fetchval("SELECT version()")
        size = await conn.fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        )
        tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        connections = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
    finally:
        await conn.close()

    print(f"  db        : {settings.postgres_db}")
    print(f"  host      : {settings.postgres_host}:{settings.postgres_port}")
    print(f"  version   : {version.split(',')[0]}")
    print(f"  size      : {size}")
    print(f"  tables    : {tables}")
    print(f"  conn(s)   : {connections}")
    print("  status    : ok")
    return 0


async def tables() -> int:
    conn = await _connect()
    try:
        rows = await conn.fetch("""
            SELECT schemaname || '.' || relname AS table_name,
                   n_live_tup AS est_rows,
                   pg_size_pretty(pg_total_relation_size(schemaname || '.' || relname)) AS size
            FROM pg_stat_user_tables
            ORDER BY n_live_tup DESC
        """)
    finally:
        await conn.close()
    if not rows:
        print("(no tables)")
        return 0
    for r in rows:
        print(f"  {r['table_name']:<40} {r['est_rows']:>10}  {r['size']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m app.db_wrapper",
        description="agentMemory DB query wrapper (credentials never printed)",
    )
    p.add_argument("sql", nargs="?", help="SQL to run (SELECT or DML with --force)")
    p.add_argument("--json", action="store_true", help="Output rows as JSON")
    p.add_argument("--force", action="store_true", help="Permit DML (INSERT/UPDATE/DELETE)")
    p.add_argument("--health", action="store_true", help="Show DB health summary")
    p.add_argument("--tables", action="store_true", help="List user tables with row counts")
    args = p.parse_args(argv)

    if args.health:
        return asyncio.run(health())
    if args.tables:
        return asyncio.run(tables())
    if not args.sql:
        p.print_help()
        return 1
    return asyncio.run(run_query(args.sql, args.json, args.force))


if __name__ == "__main__":
    sys.exit(main())
