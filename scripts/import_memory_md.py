#!/usr/bin/env python3
"""
Import .claude/projects/*/memory/*.md files as observations.

Each markdown file becomes one observation with type='discovery'.
The project path is derived from the directory name.

Usage:
    .venv/bin/python scripts/import_memory_md.py [--dry-run]
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MEMORY_ROOT = Path.home() / ".claude" / "projects"

def dir_to_project_path(dirname: str) -> str:
    """Convert claude project dir name back to actual filesystem path.

    Claude encodes paths by replacing /, _, and . with -. This is ambiguous,
    so we resolve by testing which decoded path exists on the filesystem.
    """
    if not dirname.startswith("-"):
        return dirname
    return _resolve("/", dirname[1:]) or dirname


def _resolve(base: str, remaining: str) -> str | None:
    """Recursively resolve encoded path segments against the filesystem."""
    if not remaining:
        return base
    for i in range(1, len(remaining) + 1):
        segment = remaining[:i]
        rest = remaining[i:]
        if rest and not rest.startswith("-"):
            continue
        rest = rest[1:] if rest.startswith("-") else rest
        for variant in {segment, segment.replace("-", "_"), segment.replace("-", ".")}:
            candidate = os.path.join(base, variant)
            if os.path.exists(candidate):
                result = _resolve(candidate, rest)
                if result and os.path.exists(result):
                    return result
    # Fallback: return best partial match
    return base if os.path.exists(base) else None


def find_memory_files() -> list[dict]:
    """Find all memory .md files with content."""
    results = []
    for memory_dir in MEMORY_ROOT.glob("*/memory"):
        project_dir = memory_dir.parent.name
        project_path = dir_to_project_path(project_dir)

        for md_file in memory_dir.glob("*.md"):
            content = md_file.read_text(encoding="utf-8", errors="replace").strip()
            if len(content) < 50:
                continue

            results.append({
                "project_path": project_path,
                "project_name": Path(project_path).name or project_dir,
                "filename": md_file.name,
                "title": f"Memory: {md_file.stem} ({Path(project_path).name})",
                "content": content,
            })

    return results


async def import_files(dry_run: bool = False):
    import asyncpg

    files = find_memory_files()
    if not files:
        logger.info("No memory files found")
        return

    logger.info(f"Found {len(files)} memory files to import")
    for f in files:
        logger.info(f"  {f['project_name']}/{f['filename']} ({len(f['content'])} chars)")

    if dry_run:
        return

    dsn = settings.effective_database_url.replace("postgresql://", "postgres://", 1)
    conn = await asyncpg.connect(dsn)

    try:
        # Get or create manual session
        srow = await conn.fetchrow("SELECT id FROM mem_sessions WHERE session_id = 'memory-md-import'")
        if not srow:
            # Need a project first
            prow = await conn.fetchrow(
                "INSERT INTO mem_projects (name, full_path) VALUES ('memory-import', 'memory-import') "
                "ON CONFLICT (full_path) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            )
            srow = await conn.fetchrow(
                "INSERT INTO mem_sessions (session_id, project_id, agent_type, status) "
                "VALUES ('memory-md-import', $1, 'import', 'completed') RETURNING id",
                prow["id"],
            )
        session_id = srow["id"]

        # Import embeddings
        from app.embeddings import embed_text_sync
        model_row = await conn.fetchrow(
            "SELECT id FROM embedding_models WHERE is_default = true LIMIT 1"
        )
        model_id = model_row["id"] if model_row else None

        imported = 0
        for f in files:
            # Get or create project
            prow = await conn.fetchrow("SELECT id FROM mem_projects WHERE full_path = $1", f["project_path"])
            if not prow:
                prow = await conn.fetchrow(
                    "INSERT INTO mem_projects (name, full_path) VALUES ($1, $2) "
                    "ON CONFLICT (full_path) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                    f["project_name"], f["project_path"],
                )
            project_id = prow["id"]

            # Check for existing import (dedup by title)
            exists = await conn.fetchval(
                "SELECT 1 FROM mem_observations WHERE title = $1 AND project_id = $2",
                f["title"], project_id,
            )
            if exists:
                logger.info(f"  Skipping (already exists): {f['title']}")
                continue

            # Generate embedding
            embedding_str = None
            try:
                # Use first 1000 chars for embedding (model has token limit)
                emb_text = f["content"][:2000]
                embedding = embed_text_sync(emb_text)
                embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
            except Exception as e:
                logger.warning(f"  Embedding failed: {e}")

            await conn.execute("""
                INSERT INTO mem_observations (
                    session_id, project_id, title, subtitle, type,
                    narrative, raw_text, embedding, embedding_model_id,
                    tool_name, created_at
                ) VALUES ($1, $2, $3, $4, 'discovery', $5, $6, $7::vector, $8, 'import', now())
            """,
                session_id, project_id,
                f["title"],
                f"Imported from {f['filename']}",
                f["content"][:2000],  # narrative
                f["content"],  # raw_text (full)
                embedding_str, model_id,
            )
            imported += 1
            logger.info(f"  Imported: {f['title']}")

        logger.info(f"Import complete: {imported} files imported")

    finally:
        await conn.close()


def main():
    dry_run = "--dry-run" in sys.argv
    asyncio.run(import_files(dry_run=dry_run))


if __name__ == "__main__":
    main()
