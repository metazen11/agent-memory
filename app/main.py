import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import init_pool, close_pool, get_pool
from app.migrate import run_migrations_with_pool
from app.queue_worker import start_worker, stop_worker
from app.routes.health import router as health_router
from app.routes.observations import router as observations_router
from app.routes.sessions import router as sessions_router
from app.routes.admin import router as admin_router
from app.routes.lessons import router as lessons_router
from app.routes.prompts import router as prompts_router

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

    # Warn on passwordless PostgreSQL
    if not settings.postgres_password and not settings.allow_trust_auth:
        logger.critical(
            "PostgreSQL password is empty. Set POSTGRES_PASSWORD in .env "
            "or ALLOW_TRUST_AUTH=true to suppress this warning."
        )

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

# CORS middleware
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Auth middleware (opt-in via REQUIRE_AUTH=true)
if settings.require_auth:
    from app.middleware import AuthMiddleware
    app.add_middleware(AuthMiddleware)

# Rate limiting
if settings.rate_limit_enabled:
    from app.middleware import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)

# Audit logging
if settings.audit_log_level != "off":
    from app.middleware import AuditMiddleware
    app.add_middleware(AuditMiddleware)

app.include_router(health_router)
app.include_router(observations_router)
app.include_router(sessions_router)
app.include_router(admin_router)
app.include_router(lessons_router)
app.include_router(prompts_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
