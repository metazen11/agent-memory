import asyncpg
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# Module-level pool, initialized on startup
pool: asyncpg.Pool | None = None


async def init_pool():
    """Create asyncpg connection pool. Call during FastAPI startup."""
    global pool
    # Convert SQLAlchemy-style URL to asyncpg format
    dsn = settings.database_url
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    logger.info("Database pool initialized")


async def close_pool():
    """Close pool. Call during FastAPI shutdown."""
    global pool
    if pool:
        await pool.close()
        pool = None
        logger.info("Database pool closed")


async def get_pool() -> asyncpg.Pool:
    """Get the connection pool (FastAPI dependency)."""
    if pool is None:
        raise RuntimeError("Database pool not initialized")
    return pool


async def execute_sql_file(path: str):
    """Execute a .sql file against the database."""
    if pool is None:
        raise RuntimeError("Database pool not initialized")
    with open(path) as f:
        sql = f.read()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info(f"Executed SQL file: {path}")
