import asyncio
import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db import get_pool
from app.embeddings import embed_text
from app.observation_llm import generate_observation, SKIP_TOOLS

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None


async def process_one(pool) -> bool:
    """Dequeue and process one pending observation.

    Returns True if an item was processed, False if queue is empty.
    """
    async with pool.acquire() as conn:
        # Atomically claim one pending item
        row = await conn.fetchrow("""
            UPDATE mem_observation_queue
            SET status = 'processing'
            WHERE id = (
                SELECT id FROM mem_observation_queue
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, session_id, tool_name, tool_input,
                      tool_response_preview, cwd, last_user_message
        """)

        if row is None:
            return False

        queue_id = row["id"]
        tool_name = row["tool_name"] or ""

        # Skip low-value tools
        if tool_name in SKIP_TOOLS:
            await conn.execute(
                "UPDATE mem_observation_queue SET status = 'skipped', processed_at = now() WHERE id = $1",
                queue_id,
            )
            logger.debug(f"Skipped tool {tool_name} (queue #{queue_id})")
            return True

        try:
            # Generate observation via LLM
            tool_input = json.loads(row["tool_input"]) if row["tool_input"] else None
            obs_data = await generate_observation(
                tool_name=tool_name,
                tool_input=tool_input,
                tool_response_preview=row["tool_response_preview"],
                cwd=row["cwd"],
                last_user_message=row["last_user_message"],
            )

            if obs_data is None:
                await conn.execute(
                    "UPDATE mem_observation_queue SET status = 'skipped', processed_at = now() WHERE id = $1",
                    queue_id,
                )
                logger.debug(f"LLM skipped observation for queue #{queue_id}")
                return True

            # Get project_id from session
            session_row = await conn.fetchrow(
                "SELECT project_id FROM mem_sessions WHERE id = $1",
                row["session_id"],
            )
            if not session_row:
                raise ValueError(f"Session {row['session_id']} not found")

            project_id = session_row["project_id"]

            # Build raw_text for embedding
            raw_text = _build_raw_text(obs_data)

            # Generate embedding
            embedding = None
            embedding_model_id = None
            try:
                embedding = await embed_text(raw_text)
                model_row = await conn.fetchrow(
                    "SELECT id FROM embedding_models WHERE is_default = true LIMIT 1"
                )
                if model_row:
                    embedding_model_id = model_row["id"]
            except Exception as e:
                logger.warning(f"Embedding failed for queue #{queue_id}: {e}")

            # Build embedding string for pgvector
            embedding_str = None
            if embedding:
                embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

            # Insert observation
            await conn.execute("""
                INSERT INTO mem_observations (
                    session_id, project_id, title, subtitle, type,
                    narrative, facts, concepts, files_read, files_modified,
                    raw_text, embedding, embedding_model_id,
                    tool_name, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10,
                    $11, $12::vector, $13,
                    $14, now()
                )
            """,
                row["session_id"],
                project_id,
                obs_data.get("title", "Untitled"),
                obs_data.get("subtitle"),
                obs_data.get("type", "discovery"),
                obs_data.get("narrative"),
                json.dumps(obs_data.get("facts", [])),
                json.dumps(obs_data.get("concepts", [])),
                json.dumps(obs_data.get("files_read", [])),
                json.dumps(obs_data.get("files_modified", [])),
                raw_text,
                embedding_str,
                embedding_model_id,
                tool_name,
            )

            # Mark queue item done
            await conn.execute(
                "UPDATE mem_observation_queue SET status = 'done', processed_at = now() WHERE id = $1",
                queue_id,
            )
            logger.info(f"Created observation from queue #{queue_id}: {obs_data.get('title', '?')}")
            return True

        except Exception as e:
            logger.error(f"Queue #{queue_id} processing failed: {e}")
            await conn.execute("""
                UPDATE mem_observation_queue
                SET status = CASE WHEN retry_count >= $2 THEN 'failed' ELSE 'pending' END,
                    retry_count = retry_count + 1,
                    processed_at = now()
                WHERE id = $1
            """, queue_id, settings.queue_max_retries)
            return True


def _build_raw_text(obs_data: dict) -> str:
    """Combine observation fields into searchable raw text."""
    parts = [obs_data.get("title", "")]
    if obs_data.get("subtitle"):
        parts.append(obs_data["subtitle"])
    if obs_data.get("narrative"):
        parts.append(obs_data["narrative"])
    for fact in obs_data.get("facts", []):
        parts.append(f"- {fact}")
    return "\n".join(parts)


async def worker_loop():
    """Background loop that processes the observation queue."""
    logger.info("Queue worker started")
    while True:
        try:
            pool = await get_pool()
            had_work = await process_one(pool)
            if not had_work:
                await asyncio.sleep(settings.queue_poll_interval)
        except asyncio.CancelledError:
            logger.info("Queue worker stopped")
            return
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
            await asyncio.sleep(settings.queue_poll_interval)


def start_worker():
    """Start the background queue worker task."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(worker_loop())
        logger.info("Queue worker task created")


def stop_worker():
    """Stop the background queue worker task."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        logger.info("Queue worker task cancelled")
