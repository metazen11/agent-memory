"""Shared fixtures for migration tests.

Migration tests are first-of-their-kind in this repo. Pattern documented here
so future ``NNN-*.sql`` migrations can copy it.

Approach
--------
Each test module gets its OWN dedicated, throwaway Postgres database. The
fixture below:

1. Connects to the ``postgres`` maintenance DB as the superuser the local
   developer has access to (uses the OS user when ``PGUSER`` is unset, which
   matches the project's ``psql -U mz`` convention).
2. Creates a database named ``agent_memory_test_<test_module>_<random>``.
3. Installs the ``vector`` extension required by ``001-initial-schema.sql``.
4. Yields the DSN. Tests apply whatever migrations they want via
   ``app.migrate.run_migrations``.
5. After the module finishes, drops the database.

Why a real DB and not transactional rollback
---------------------------------------------
Migration 012 uses ``CREATE INDEX CONCURRENTLY``, which cannot run inside a
transaction. Wrapping each test in ``BEGIN; ...; ROLLBACK`` would mask the
real concurrent-section behaviour. The throwaway-DB pattern lets us exercise
the runner end-to-end exactly as production does.

Environment knobs
-----------------
- ``AGENT_MEMORY_TEST_PG_DSN`` overrides the maintenance DSN. Default:
  ``postgresql://localhost:5432/postgres`` (asyncpg uses the OS user by
  default).
"""

import asyncio
import os
import secrets
from typing import AsyncIterator

import asyncpg
import pytest


def _maintenance_dsn() -> str:
    """DSN for the ``postgres`` maintenance database used to CREATE/DROP."""
    return os.environ.get(
        "AGENT_MEMORY_TEST_PG_DSN",
        "postgresql://localhost:5432/postgres",
    )


def _swap_database(dsn: str, db_name: str) -> str:
    """Return ``dsn`` with the database path replaced by ``db_name``."""
    # Strip the path component; works for ``postgres://host[:port]/db`` and
    # ``postgresql://host[:port]/db?query``.
    base = dsn.rsplit("/", 1)[0]
    # Preserve any query string from the original.
    if "?" in dsn:
        _, query = dsn.split("?", 1)
        return f"{base}/{db_name}?{query}"
    return f"{base}/{db_name}"


@pytest.fixture(scope="module")
async def throwaway_db(request) -> AsyncIterator[str]:
    """Yield a DSN for a per-module, freshly created Postgres database.

    The database is created with the ``vector`` extension preinstalled (needed
    by ``001-initial-schema.sql``). After all tests in the module finish, the
    database is dropped.
    """
    maintenance_dsn = _maintenance_dsn()
    suffix = secrets.token_hex(4)
    # PG identifiers max 63 chars; stay well under.
    module_tag = request.module.__name__.rsplit(".", 1)[-1][:30]
    db_name = f"agentmem_test_{module_tag}_{suffix}"

    admin = await asyncpg.connect(maintenance_dsn)
    try:
        await admin.execute(
            f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
        )
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    db_dsn = _swap_database(maintenance_dsn, db_name)

    # Install required extensions in the new DB.
    conn = await asyncpg.connect(db_dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()

    try:
        yield db_dsn
    finally:
        admin = await asyncpg.connect(maintenance_dsn)
        try:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
            )
        finally:
            await admin.close()
