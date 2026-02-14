import asyncio
import logging

from fastapi import APIRouter

from app.db import get_pool
from app.embeddings import embed_batch_sync

logger = logging.getLogger(__name__)

router = APIRouter()

# Background re-embed task tracking
_reembed_task: asyncio.Task | None = None
_reembed_status: dict = {}


@router.get("/api/admin/stats")
async def admin_stats():
    """Overview statistics."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        obs_count = await conn.fetchval("SELECT count(*) FROM mem_observations")
        obs_with_emb = await conn.fetchval(
            "SELECT count(*) FROM mem_observations WHERE embedding IS NOT NULL"
        )
        session_count = await conn.fetchval("SELECT count(*) FROM mem_sessions")
        project_count = await conn.fetchval("SELECT count(*) FROM mem_projects")
        queue_pending = await conn.fetchval(
            "SELECT count(*) FROM mem_observation_queue WHERE status = 'pending'"
        )
        queue_failed = await conn.fetchval(
            "SELECT count(*) FROM mem_observation_queue WHERE status = 'failed'"
        )
        queue_done = await conn.fetchval(
            "SELECT count(*) FROM mem_observation_queue WHERE status = 'done'"
        )

        # Type breakdown
        type_rows = await conn.fetch(
            "SELECT type, count(*) as cnt FROM mem_observations GROUP BY type ORDER BY cnt DESC"
        )
        type_breakdown = {r["type"]: r["cnt"] for r in type_rows}

        # Project breakdown
        proj_rows = await conn.fetch("""
            SELECT p.name, count(*) as cnt
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            GROUP BY p.name ORDER BY cnt DESC
        """)
        project_breakdown = {r["name"]: r["cnt"] for r in proj_rows}

        return {
            "observations": {
                "total": obs_count,
                "with_embedding": obs_with_emb,
                "without_embedding": obs_count - obs_with_emb,
            },
            "sessions": session_count,
            "projects": project_count,
            "queue": {
                "pending": queue_pending,
                "done": queue_done,
                "failed": queue_failed,
            },
            "by_type": type_breakdown,
            "by_project": project_breakdown,
        }


async def _reembed_worker(only_missing: bool, batch_size: int):
    """Background worker that re-embeds observations in batches."""
    global _reembed_status
    from app.config import settings
    import time

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get or create model record
        model_row = await conn.fetchrow(
            "SELECT id FROM embedding_models WHERE model_name = $1",
            settings.embedding_model,
        )
        if not model_row:
            from app.embeddings import _get_model
            model = _get_model()
            dims = model.get_sentence_embedding_dimension()
            model_row = await conn.fetchrow(
                """INSERT INTO embedding_models (model_name, dimensions, provider, is_default)
                   VALUES ($1, $2, 'sentence-transformers', true)
                   ON CONFLICT (model_name) DO UPDATE SET is_default = true
                   RETURNING id""",
                settings.embedding_model, dims,
            )
        model_id = model_row["id"]

        where = "WHERE embedding IS NULL" if only_missing else ""
        total = await conn.fetchval(f"SELECT count(*) FROM mem_observations {where}")

        _reembed_status = {
            "status": "running",
            "total": total,
            "processed": 0,
            "errors": 0,
            "model": settings.embedding_model,
            "only_missing": only_missing,
        }

        last_id = 0
        start_time = time.time()

        while True:
            # Cursor-based pagination by ID — immune to row shifts
            if only_missing:
                rows = await conn.fetch(
                    "SELECT id, raw_text FROM mem_observations WHERE embedding IS NULL AND id > $1 ORDER BY id LIMIT $2",
                    last_id, batch_size,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, raw_text FROM mem_observations WHERE id > $1 ORDER BY id LIMIT $2",
                    last_id, batch_size,
                )
            if not rows:
                break

            texts = [r["raw_text"] for r in rows]
            ids = [r["id"] for r in rows]

            try:
                loop = asyncio.get_event_loop()
                vectors = await loop.run_in_executor(None, embed_batch_sync, texts)

                for row_id, vec in zip(ids, vectors):
                    emb_str = "[" + ",".join(str(v) for v in vec) + "]"
                    await conn.execute(
                        "UPDATE mem_observations SET embedding = $1::vector, embedding_model_id = $2 WHERE id = $3",
                        emb_str, model_id, row_id,
                    )
                _reembed_status["processed"] += len(vectors)
            except asyncio.CancelledError:
                _reembed_status["status"] = "cancelled"
                logger.info("Re-embed cancelled")
                return
            except Exception as e:
                logger.error(f"Re-embed batch failed after id {last_id}: {e}")
                _reembed_status["errors"] += len(rows)

            last_id = ids[-1]
            elapsed = time.time() - start_time
            rate = _reembed_status["processed"] / elapsed if elapsed > 0 else 0
            _reembed_status["rate"] = round(rate, 1)
            logger.info(
                f"Re-embed progress: {_reembed_status['processed']}/{total} "
                f"({rate:.0f}/sec)"
            )

    elapsed = time.time() - start_time
    _reembed_status["status"] = "done"
    _reembed_status["elapsed_seconds"] = round(elapsed, 1)
    logger.info(
        f"Re-embed complete: {_reembed_status['processed']} done, "
        f"{_reembed_status['errors']} errors in {elapsed:.1f}s"
    )


@router.post("/api/admin/re-embed")
async def re_embed(only_missing: bool = True, batch_size: int = 100):
    """Queue a background re-embed job.

    Runs asynchronously — check progress via GET /api/admin/re-embed/status.
    """
    global _reembed_task

    if _reembed_task and not _reembed_task.done():
        return {"error": "Re-embed already in progress", "status": _reembed_status}

    _reembed_task = asyncio.create_task(_reembed_worker(only_missing, batch_size))
    return {"status": "started", "only_missing": only_missing, "batch_size": batch_size}


@router.get("/api/admin/re-embed/status")
async def re_embed_status():
    """Check re-embed job progress."""
    if not _reembed_status:
        return {"status": "idle"}
    return _reembed_status


@router.post("/api/admin/re-embed/cancel")
async def re_embed_cancel():
    """Cancel a running re-embed job."""
    global _reembed_task
    if _reembed_task and not _reembed_task.done():
        _reembed_task.cancel()
        return {"status": "cancelling"}
    return {"status": "no job running"}
