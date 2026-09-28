from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app import llm_provider_status
from app.config import settings
from app.db import get_pool
from app.embeddings import check_embeddings

router = APIRouter()


@router.get("/api/health")
async def health():
    """Health check: DB connectivity, embedding model, queue depth, LLM provider."""
    result = {"db": {}, "embeddings": {}, "queue": {}, "llm": {}}

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

    # LLM provider status — LAST KNOWN state only, never a live call.
    #
    # This section exists because credit exhaustion used to be invisible.
    # When the Anthropic key returned "Your credit balance is too low",
    # lesson synthesis and observation capture silently fell back to the
    # local 7B (lower quality) with nothing but a per-call log warning; an
    # operator could not tell that from "no key configured".
    #
    # Deliberately NOT a live probe: a health check that calls the API
    # would bill the account on every poll and would wait out the 13s
    # rate limiter. snapshot() reads only in-process state recorded by the
    # real calls, so it is free and cannot itself fail.
    result["llm"] = {
        "local_model_configured": bool(settings.observation_llm_model),
        "providers": [llm_provider_status.snapshot(settings.anthropic_api_key)],
    }

    # Overall status
    db_ok = result["db"].get("status") == "ok"
    emb_ok = result["embeddings"].get("status") == "ok"
    # A tripped provider breaker degrades the service: capture and
    # distillation still work via the local model, but at reduced quality,
    # which is exactly the condition an operator needs to see. A provider
    # that is simply not configured is a deployment choice, not a fault,
    # so it does NOT degrade the overall status.
    llm_degraded = any(
        p.get("circuit_open") for p in result["llm"]["providers"]
    )
    result["status"] = "ok" if db_ok and emb_ok and not llm_degraded else "degraded"

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
