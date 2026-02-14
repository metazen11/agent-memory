#!/usr/bin/env python3
"""
Re-embed all observations using sentence-transformers (in-process).

Usage:
    cd /Users/mz/Dropbox/_CODING/agentMemory
    source .venv/bin/activate
    python scripts/re_embed.py [--batch-size 100] [--only-missing]

Options:
    --batch-size N   Process N observations at a time (default: 100)
    --only-missing   Only embed observations without an existing embedding
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings
from app.embeddings import embed_batch_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def re_embed(batch_size: int = 100, only_missing: bool = False):
    """Re-embed observations using sentence-transformers batch processing."""
    import asyncpg

    dsn = settings.database_url.replace("postgresql://", "postgres://", 1)
    conn = await asyncpg.connect(dsn)

    try:
        # Get or create model record
        model_row = await conn.fetchrow(
            "SELECT id, dimensions FROM embedding_models WHERE model_name = $1",
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
                   RETURNING id, dimensions""",
                settings.embedding_model, dims,
            )
        model_id = model_row["id"]

        where = "WHERE embedding IS NULL" if only_missing else ""
        total = await conn.fetchval(f"SELECT count(*) FROM mem_observations {where}")
        logger.info(f"Re-embedding {total} observations with '{settings.embedding_model}' (batch_size={batch_size})")

        processed = 0
        errors = 0
        last_id = 0
        start_time = time.time()

        while True:
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
                vectors = embed_batch_sync(texts)

                for row_id, vec in zip(ids, vectors):
                    emb_str = "[" + ",".join(str(v) for v in vec) + "]"
                    await conn.execute(
                        "UPDATE mem_observations SET embedding = $1::vector, embedding_model_id = $2 WHERE id = $3",
                        emb_str, model_id, row_id,
                    )
                processed += len(vectors)
            except Exception as e:
                logger.error(f"Batch failed at offset {offset}: {e}")
                errors += batch_size

            last_id = ids[-1]
            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            logger.info(
                f"Progress: {processed + errors}/{total} "
                f"(ok={processed}, err={errors}, {rate:.0f}/sec)"
            )

        elapsed = time.time() - start_time
        logger.info(f"Done: {processed} re-embedded, {errors} errors in {elapsed:.1f}s")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Re-embed observations with sentence-transformers")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--only-missing", action="store_true", help="Only embed missing embeddings")
    args = parser.parse_args()

    asyncio.run(re_embed(batch_size=args.batch_size, only_missing=args.only_missing))


if __name__ == "__main__":
    main()
