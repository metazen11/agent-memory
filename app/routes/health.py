from fastapi import APIRouter

from app.db import get_pool
from app.embeddings import check_embeddings

router = APIRouter()


@router.get("/api/health")
async def health():
    """Health check: DB connectivity, embedding model, queue depth."""
    result = {"db": {}, "embeddings": {}, "queue": {}}

    # DB check
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
            has_vector = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )
            queue_pending = await conn.fetchval(
                "SELECT count(*) FROM mem_observation_queue WHERE status = 'pending'"
            ) or 0
            obs_count = await conn.fetchval(
                "SELECT count(*) FROM mem_observations"
            ) or 0
        result["db"] = {
            "status": "ok",
            "version": version.split(",")[0] if version else "unknown",
            "pgvector": has_vector,
        }
        result["queue"] = {"pending": queue_pending, "observations_total": obs_count}
    except Exception as e:
        result["db"] = {"status": "error", "error": str(e)}
        result["queue"] = {"pending": -1, "observations_total": -1}

    # Embedding model check
    result["embeddings"] = await check_embeddings()

    # Overall status
    db_ok = result["db"].get("status") == "ok"
    emb_ok = result["embeddings"].get("status") == "ok"
    result["status"] = "ok" if db_ok and emb_ok else "degraded"

    return result
