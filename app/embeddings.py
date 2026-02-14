import asyncio
import logging
from functools import lru_cache

from app.config import settings

logger = logging.getLogger(__name__)

_model = None


def _get_model():
    """Lazy-load the sentence-transformers model (cached singleton)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading embedding model: {settings.embedding_model}")
        _model = SentenceTransformer(
            settings.embedding_model,
            trust_remote_code=True,
        )
        logger.info(f"Embedding model loaded ({_model.get_sentence_embedding_dimension()}d)")
    return _model


def embed_text_sync(text: str) -> list[float]:
    """Generate embedding vector synchronously (in-process, no HTTP)."""
    model = _get_model()
    vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def embed_batch_sync(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts (much faster than one-at-a-time)."""
    model = _get_model()
    vectors = model.encode(texts, normalize_embeddings=True, batch_size=64)
    return [v.tolist() for v in vectors]


async def embed_text(text: str) -> list[float]:
    """Generate embedding vector (runs in thread pool to avoid blocking)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed_text_sync, text)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts async."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed_batch_sync, texts)


async def check_embeddings() -> dict:
    """Check embedding model availability."""
    try:
        model = _get_model()
        dims = model.get_sentence_embedding_dimension()
        return {
            "status": "ok",
            "model": settings.embedding_model,
            "dimensions": dims,
            "provider": "sentence-transformers",
        }
    except Exception as e:
        logger.warning(f"Embedding model check failed: {e}")
        return {"status": "error", "error": str(e)}
