#!/usr/bin/env python3
"""
Migrate observations from claude-mem SQLite database to agent-memory PostgreSQL.

Usage:
    cd /Users/mz/Dropbox/_CODING/agentMemory
    source .venv/bin/activate
    python scripts/migrate_claude_mem.py [--embed] [--batch-size 50]

Options:
    --embed         Generate embeddings during migration (slow but complete)
    --batch-size N  Process N observations at a time (default: 50)
    --dry-run       Show what would be migrated without writing
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent to path for app imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

CLAUDE_MEM_DB = os.path.expanduser("~/.claude-mem/claude-mem.db")

# Map claude-mem observation types to our types
TYPE_MAP = {
    "decision": "decision",
    "bugfix": "bugfix",
    "feature": "feature",
    "refactor": "refactor",
    "discovery": "discovery",
    "change": "change",
}


def _parse_ts(ts_str: str | None) -> datetime | None:
    """Parse ISO timestamp string to datetime."""
    if not ts_str:
        return None
    try:
        # Handle '2025-12-11T00:14:59.076Z' format
        ts_str = ts_str.rstrip("Z")
        if "." in ts_str:
            return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def read_sqlite_observations(db_path: str) -> list[dict]:
    """Read all observations from claude-mem SQLite database."""
    if not os.path.exists(db_path):
        logger.error(f"SQLite database not found: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("""
            SELECT o.id, o.title, o.subtitle, o.type, o.narrative,
                   o.facts, o.concepts, o.text as raw_text,
                   o.files_read, o.files_modified, o.project,
                   o.memory_session_id as session_id,
                   o.created_at
            FROM observations o
            ORDER BY o.created_at ASC
        """).fetchall()

        observations = []
        for row in rows:
            obs = dict(row)

            # Parse JSON fields
            for field in ("facts", "concepts", "files_read", "files_modified"):
                if obs.get(field):
                    try:
                        obs[field] = json.loads(obs[field])
                    except (json.JSONDecodeError, TypeError):
                        obs[field] = []
                else:
                    obs[field] = []

            # Map type
            obs["type"] = TYPE_MAP.get(obs.get("type", ""), "discovery")

            # Build raw_text if missing
            if not obs.get("raw_text"):
                parts = [obs.get("title", "")]
                if obs.get("subtitle"):
                    parts.append(obs["subtitle"])
                if obs.get("narrative"):
                    parts.append(obs["narrative"])
                obs["raw_text"] = "\n".join(parts)

            observations.append(obs)

        logger.info(f"Read {len(observations)} observations from SQLite")
        return observations

    finally:
        conn.close()


def read_sqlite_sessions(db_path: str) -> list[dict]:
    """Read sessions from claude-mem."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT memory_session_id as session_id, project,
                   min(created_at) as started_at
            FROM observations
            GROUP BY memory_session_id, project
            ORDER BY started_at ASC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def migrate(embed: bool = False, batch_size: int = 50, dry_run: bool = False):
    """Run the migration."""
    import asyncpg

    observations = read_sqlite_observations(CLAUDE_MEM_DB)
    if not observations:
        logger.error("No observations to migrate")
        return

    sessions = read_sqlite_sessions(CLAUDE_MEM_DB)

    if dry_run:
        logger.info(f"DRY RUN: Would migrate {len(observations)} observations from {len(sessions)} sessions")
        # Show project breakdown
        projects = {}
        for obs in observations:
            p = obs.get("project", "unknown")
            projects[p] = projects.get(p, 0) + 1
        for p, count in sorted(projects.items(), key=lambda x: -x[1]):
            logger.info(f"  {p}: {count} observations")
        return

    # Connect to Postgres
    dsn = settings.database_url.replace("postgresql://", "postgres://", 1)
    conn = await asyncpg.connect(dsn)

    try:
        # Create projects
        project_ids = {}
        for session in sessions:
            project_name = session.get("project", "unknown")
            if project_name not in project_ids:
                row = await conn.fetchrow(
                    "SELECT id FROM mem_projects WHERE name = $1", project_name
                )
                if row:
                    project_ids[project_name] = row["id"]
                else:
                    row = await conn.fetchrow(
                        "INSERT INTO mem_projects (name) VALUES ($1) RETURNING id",
                        project_name,
                    )
                    project_ids[project_name] = row["id"]

        logger.info(f"Created/found {len(project_ids)} projects")

        # Create sessions
        session_ids = {}
        for session in sessions:
            sid = session.get("session_id", "unknown")
            project_name = session.get("project", "unknown")
            if sid not in session_ids:
                row = await conn.fetchrow(
                    "SELECT id FROM mem_sessions WHERE session_id = $1", sid
                )
                if row:
                    session_ids[sid] = row["id"]
                else:
                    row = await conn.fetchrow("""
                        INSERT INTO mem_sessions (session_id, project_id, status)
                        VALUES ($1, $2, 'completed')
                        RETURNING id
                    """, sid, project_ids.get(project_name, 1))
                    session_ids[sid] = row["id"]

        logger.info(f"Created/found {len(session_ids)} sessions")

        # Get default embedding model id
        embed_model_id = None
        if embed:
            model_row = await conn.fetchrow(
                "SELECT id FROM embedding_models WHERE is_default = true LIMIT 1"
            )
            if model_row:
                embed_model_id = model_row["id"]

        # Migrate observations in batches
        migrated = 0
        skipped = 0
        errors = 0

        for i in range(0, len(observations), batch_size):
            batch = observations[i:i + batch_size]

            for obs in batch:
                try:
                    project_name = obs.get("project", "unknown")
                    session_id = obs.get("session_id", "unknown")

                    project_id = project_ids.get(project_name)
                    session_db_id = session_ids.get(session_id)

                    if not project_id or not session_db_id:
                        skipped += 1
                        continue

                    # Generate embedding if requested
                    embedding_str = None
                    if embed:
                        try:
                            from app.embeddings import embed_text
                            embedding = await embed_text(obs["raw_text"])
                            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
                        except Exception as e:
                            logger.debug(f"Embedding failed: {e}")

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
                            $14, $15::timestamptz
                        )
                    """,
                        session_db_id,
                        project_id,
                        obs.get("title", "Untitled"),
                        obs.get("subtitle"),
                        obs["type"],
                        obs.get("narrative"),
                        json.dumps(obs.get("facts", [])),
                        json.dumps(obs.get("concepts", [])),
                        json.dumps(obs.get("files_read", [])),
                        json.dumps(obs.get("files_modified", [])),
                        obs["raw_text"],
                        embedding_str,
                        embed_model_id if embedding_str else None,
                        None,  # tool_name not in claude-mem schema
                        _parse_ts(obs.get("created_at")),
                    )
                    migrated += 1

                except Exception as e:
                    logger.error(f"Failed to migrate obs #{obs.get('id', '?')}: {e}")
                    errors += 1

            logger.info(f"Progress: {migrated + skipped + errors}/{len(observations)} "
                       f"(migrated={migrated}, skipped={skipped}, errors={errors})")

        logger.info(f"Migration complete: {migrated} migrated, {skipped} skipped, {errors} errors")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate claude-mem observations to agent-memory")
    parser.add_argument("--embed", action="store_true", help="Generate embeddings during migration")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated")
    args = parser.parse_args()

    asyncio.run(migrate(embed=args.embed, batch_size=args.batch_size, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
