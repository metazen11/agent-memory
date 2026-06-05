from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app.db import get_pool
from app.embeddings import check_embeddings

router = APIRouter()


# `/health` alias so callers using the platform convention (e.g. anvil's
# vbot probe) find this without needing to know our `/api/` prefix.
@router.get("/health", include_in_schema=False)
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


# Path resolved relative to this file so a moved checkout still finds the
# doc. agent-memory/app/routes/health.py → ../../docs/INTEGRATION.md.
_INTEGRATION_DOC_PATH = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "INTEGRATION.md"
)


@router.get("/api/integration_guide", response_class=PlainTextResponse)
async def integration_guide():
    """Return the agent-memory integration guide as markdown.

    Hosts and agents can `curl` this on first contact to learn the
    contract without reading the repo. Same content as
    docs/INTEGRATION.md — that file is the source of truth.
    """
    try:
        return _INTEGRATION_DOC_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=(
                "Integration guide missing on disk. Expected at "
                f"{_INTEGRATION_DOC_PATH}. The doc lives at "
                "docs/INTEGRATION.md in the agent-memory repo; "
                "make sure your installation includes it."
            ),
        )
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read integration guide: {e}",
        )
