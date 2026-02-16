import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.config import settings
from app.db import init_pool, close_pool, get_pool
from app.migrate import run_migrations_with_pool
from app.queue_worker import start_worker, stop_worker
from app.routes.health import router as health_router
from app.routes.observations import router as observations_router
from app.routes.sessions import router as sessions_router
from app.routes.admin import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    logger.info("agent-memory starting up")
    await init_pool()

    # Run versioned migrations on startup
    try:
        pool = await get_pool()
        applied = await run_migrations_with_pool(pool)
        if applied:
            logger.info(f"Applied migrations: {', '.join(applied)}")
    except Exception as e:
        logger.error(f"Migration failed: {e}")

    # Start background queue worker
    start_worker()

    yield

    logger.info("agent-memory shutting down")
    stop_worker()
    await close_pool()


app = FastAPI(
    title="agent-memory",
    description="Lightweight LLM memory service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(observations_router)
app.include_router(sessions_router)
app.include_router(admin_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
