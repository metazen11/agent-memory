"""init_pool must survive Postgres still booting (launchd login race)."""
import asyncio
from unittest.mock import AsyncMock, patch

import asyncpg

from app import db


def test_init_pool_retries_while_db_starting_up():
    fake_pool = object()
    create = AsyncMock(side_effect=[
        asyncpg.CannotConnectNowError("the database system is starting up"),
        ConnectionRefusedError(),
        fake_pool,
    ])
    with patch.object(db.asyncpg, "create_pool", create), \
         patch.object(db.asyncio, "sleep", AsyncMock()):
        asyncio.run(db.init_pool())
    assert db.pool is fake_pool and create.await_count == 3
    db.pool = None
