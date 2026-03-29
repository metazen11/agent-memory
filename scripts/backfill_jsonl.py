#!/usr/bin/env python3
"""Backfill agent-memory observations from Claude Code JSONL session logs.

Parses JSONL files → extracts tool calls → runs through the existing
observation LLM pipeline → generates embeddings → inserts into mem_observations.

Tracks progress per-session in `backfill_log` table for stop/resume support.

Usage:
    .venv/bin/python scripts/backfill_jsonl.py --dry-run
    .venv/bin/python scripts/backfill_jsonl.py
    .venv/bin/python scripts/backfill_jsonl.py --session 08d5d131-...
    .venv/bin/python scripts/backfill_jsonl.py --jsonl-dir /path/to/logs
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.embeddings import embed_text
from app.models import normalize_observation_type
from app.observation_llm import generate_observation, SKIP_TOOLS
from app.project import ensure_project

logger = logging.getLogger(__name__)

# Default location for Claude Code JSONL logs
DEFAULT_JSONL_DIR = Path.home() / ".claude" / "projects"


# ── JSONL Parsing ─────────────────────────────────────────────

def parse_jsonl_session(filepath: str) -> dict:
    """Parse a JSONL session file and extract tool calls with context.

    Returns:
        {
            "session_id": str,
            "tool_calls": [
                {
                    "tool_use_id": str,
                    "tool_name": str,
                    "tool_input": dict,
                    "tool_response": str | None,
                    "cwd": str | None,
                    "timestamp": str,
                    "last_user_message": str | None,
                }
            ]
        }
    """
    entries = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    session_id = None
    tool_calls = []
    user_prompts = []  # collected user messages for mem_user_prompts
    prompt_number = 0

    # Track last user message for context
    last_user_message = None

    # Build a map of tool_use_id → tool_call dict for result matching
    pending_results = {}  # tool_use_id → index in tool_calls

    for entry in entries:
        if session_id is None and entry.get("sessionId"):
            session_id = entry["sessionId"]

        msg = entry.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")
        entry_cwd = entry.get("cwd")
        entry_ts = entry.get("timestamp")

            # Track last user message text + collect user prompts
        if role == "user":
            user_text = None
            if isinstance(content, str) and content.strip():
                user_text = content.strip()
            elif isinstance(content, list):
                # Extract text parts, skip tool_result blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            # Match tool results to pending tool calls
                            tuid = block.get("tool_use_id")
                            if tuid and tuid in pending_results:
                                idx = pending_results.pop(tuid)
                                result_content = block.get("content", "")
                                if isinstance(result_content, list):
                                    # Extract text from content blocks
                                    parts = []
                                    for rc in result_content:
                                        if isinstance(rc, dict) and rc.get("type") == "text":
                                            parts.append(rc.get("text", ""))
                                    result_content = "\n".join(parts)
                                if isinstance(result_content, str):
                                    tool_calls[idx]["tool_response"] = result_content[:3000]
                    elif isinstance(block, str):
                        text_parts.append(block)
                joined = "\n".join(text_parts).strip()
                if joined:
                    user_text = joined

            if user_text:
                last_user_message = user_text
                # Collect as user prompt if it's real human text
                # (skip messages that are only system-reminder tags)
                cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", user_text, flags=re.DOTALL).strip()
                if cleaned and len(cleaned) > 1:
                    prompt_number += 1
                    user_prompts.append({
                        "prompt_number": prompt_number,
                        "prompt_text": cleaned[:10000],  # cap at 10k chars
                        "timestamp": entry_ts,
                    })

        # Extract tool_use blocks from assistant messages
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tc = {
                        "tool_use_id": block.get("id", ""),
                        "tool_name": block.get("name", ""),
                        "tool_input": block.get("input", {}),
                        "tool_response": None,
                        "cwd": entry_cwd,
                        "timestamp": entry_ts,
                        "last_user_message": last_user_message,
                    }
                    tool_calls.append(tc)
                    pending_results[tc["tool_use_id"]] = len(tool_calls) - 1

    # Derive session_id from filename if not found in entries
    if not session_id:
        session_id = Path(filepath).stem

    return {"session_id": session_id, "tool_calls": tool_calls, "user_prompts": user_prompts}


def discover_jsonl_files(base_dir: str, session_filter: str | None = None) -> list[dict]:
    """Find all JSONL session files under base_dir (excluding subagents).

    Returns list of {"path": str, "session_id": str, "size_kb": float}.
    """
    results = []
    base = Path(base_dir)
    for jsonl_path in sorted(base.rglob("*.jsonl")):
        # Skip subagent logs
        if "subagents" in str(jsonl_path):
            continue
        sid = jsonl_path.stem
        if session_filter and sid != session_filter:
            continue
        results.append({
            "path": str(jsonl_path),
            "session_id": sid,
            "size_kb": jsonl_path.stat().st_size / 1024,
        })
    return results


# ── Database helpers ──────────────────────────────────────────

async def get_connection():
    """Get a single asyncpg connection."""
    import asyncpg
    dsn = settings.effective_database_url
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)
    return await asyncpg.connect(dsn)


async def get_or_create_session(conn, session_id: str, cwd: str | None, timestamp: str | None) -> int:
    """Ensure a mem_sessions row exists for this session. Returns internal id."""
    row = await conn.fetchrow(
        "SELECT id FROM mem_sessions WHERE session_id = $1", session_id
    )
    if row:
        return row["id"]

    # Determine project from cwd
    project_path = cwd or "/unknown"
    project_id = await ensure_project(conn, project_path)

    row = await conn.fetchrow("""
        INSERT INTO mem_sessions (session_id, project_id, agent_type, status)
        VALUES ($1, $2, 'claude-code', 'completed')
        ON CONFLICT (session_id) DO UPDATE SET session_id = EXCLUDED.session_id
        RETURNING id
    """, session_id, project_id)
    return row["id"]


async def get_embedding_model_id(conn) -> int | None:
    """Get the default embedding model id."""
    row = await conn.fetchrow(
        "SELECT id FROM embedding_models WHERE is_default = true LIMIT 1"
    )
    return row["id"] if row else None


# ── Processing pipeline ──────────────────────────────────────

def build_raw_text(obs_data: dict) -> str:
    """Combine observation fields into searchable raw text."""
    parts = [obs_data.get("title", "")]
    if obs_data.get("subtitle"):
        parts.append(obs_data["subtitle"])
    if obs_data.get("narrative"):
        parts.append(obs_data["narrative"])
    for fact in obs_data.get("facts", []):
        parts.append(f"- {fact}")
    return "\n".join(parts)


async def process_tool_call(
    conn,
    tc: dict,
    session_db_id: int,
    project_id: int,
    embedding_model_id: int | None,
    dry_run: bool = False,
) -> str:
    """Process a single tool call through the observation pipeline.

    Returns: "processed", "skipped", or "error"
    """
    tool_name = tc["tool_name"]

    if tool_name in SKIP_TOOLS:
        return "skipped"

    try:
        # Generate observation via LLM
        obs_data = await generate_observation(
            tool_name=tool_name,
            tool_input=tc.get("tool_input"),
            tool_response_preview=tc.get("tool_response"),
            cwd=tc.get("cwd"),
            last_user_message=tc.get("last_user_message"),
        )

        if obs_data is None:
            return "skipped"

        if dry_run:
            logger.info(f"  [dry-run] Would create: {obs_data.get('title', '?')}")
            return "processed"

        # Build raw text and embedding
        raw_text = build_raw_text(obs_data)
        embedding_str = None
        try:
            embedding = await embed_text(raw_text)
            embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")

        # Parse timestamp
        created_at = datetime.now(timezone.utc)
        if tc.get("timestamp"):
            try:
                ts = tc["timestamp"]
                # Handle ISO format with Z suffix
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                created_at = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                pass

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
                $14, $15
            )
        """,
            session_db_id,
            project_id,
            obs_data.get("title", "Untitled"),
            obs_data.get("subtitle"),
            normalize_observation_type(obs_data.get("type", "discovery")),
            obs_data.get("narrative"),
            json.dumps(obs_data.get("facts", [])),
            json.dumps(obs_data.get("concepts", [])),
            json.dumps(obs_data.get("files_read", [])),
            json.dumps(obs_data.get("files_modified", [])),
            raw_text,
            embedding_str,
            embedding_model_id,
            tool_name,
            created_at,
        )

        logger.info(f"  Created: {obs_data.get('title', '?')}")
        return "processed"

    except Exception as e:
        logger.error(f"  Error processing {tool_name}: {e}")
        return "error"


async def process_session(
    conn,
    session_file: dict,
    dry_run: bool = False,
    batch_size: int = 10,
) -> dict:
    """Process all tool calls from one JSONL session file.

    Returns: {"processed": int, "skipped": int, "errors": int, "total": int}
    """
    filepath = session_file["path"]
    sid = session_file["session_id"]

    # Check backfill_log for resume
    log_row = await conn.fetchrow(
        "SELECT status, last_processed_idx, processed, skipped, errors FROM backfill_log WHERE session_id = $1", sid
    )
    if log_row and log_row["status"] == "done":
        logger.info(f"Skipping {sid[:12]}... (already done)")
        return {"processed": 0, "skipped": 0, "errors": 0, "total": 0, "status": "already_done"}

    # Resume point: skip tool calls already processed in a previous run
    resume_from_idx = 0
    resume_processed = 0
    resume_skipped = 0
    resume_errors = 0
    if log_row and log_row["status"] == "in_progress":
        resume_from_idx = log_row["last_processed_idx"]
        resume_processed = log_row["processed"] or 0
        resume_skipped = log_row["skipped"] or 0
        resume_errors = log_row["errors"] or 0
        if resume_from_idx > 0:
            logger.info(f"Resuming {sid[:12]}... from tool call #{resume_from_idx}")

    logger.info(f"Parsing {filepath}")
    parsed = parse_jsonl_session(filepath)
    tool_calls = parsed["tool_calls"]
    total = len(tool_calls)

    if total == 0:
        # Still insert user prompts even if no tool calls
        user_prompts = parsed.get("user_prompts", [])
        if not dry_run and user_prompts:
            first_cwd = None
            session_db_id = await get_or_create_session(conn, sid, first_cwd, None)
            existing = await conn.fetchval(
                "SELECT count(*) FROM mem_user_prompts WHERE session_id = $1", session_db_id
            )
            if existing == 0:
                for up in user_prompts:
                    await conn.execute("""
                        INSERT INTO mem_user_prompts (session_id, prompt_number, prompt_text, created_at)
                        VALUES ($1, $2, $3, now())
                    """, session_db_id, up["prompt_number"], up["prompt_text"])
                logger.info(f"  No tool calls but inserted {len(user_prompts)} user prompts")
        else:
            logger.info(f"  No tool calls found, skipping")
        return {"processed": 0, "skipped": 0, "errors": 0, "total": 0, "status": "empty"}

    logger.info(f"  Found {total} tool calls in session {sid[:12]}...")

    # Determine project from first tool call with a cwd
    first_cwd = None
    for tc in tool_calls:
        if tc.get("cwd"):
            first_cwd = tc["cwd"]
            break

    if not dry_run:
        # Ensure session exists in DB
        session_db_id = await get_or_create_session(conn, sid, first_cwd, tool_calls[0].get("timestamp"))

        # Get project_id from session
        sess_row = await conn.fetchrow("SELECT project_id FROM mem_sessions WHERE id = $1", session_db_id)
        project_id = sess_row["project_id"]

        embedding_model_id = await get_embedding_model_id(conn)

        # Upsert backfill_log (preserve counters on resume)
        if resume_from_idx > 0:
            await conn.execute("""
                UPDATE backfill_log SET status = 'in_progress' WHERE session_id = $1
            """, sid)
        else:
            await conn.execute("""
                INSERT INTO backfill_log (session_id, jsonl_path, total_tools, status, started_at)
                VALUES ($1, $2, $3, 'in_progress', now())
                ON CONFLICT (session_id) DO UPDATE SET
                    status = 'in_progress', started_at = now(),
                    processed = 0, skipped = 0, errors = 0, last_processed_idx = 0
            """, sid, filepath, total)
    else:
        session_db_id = 0
        project_id = 0
        embedding_model_id = None

    processed = resume_processed
    skipped = resume_skipped
    errors = resume_errors

    for i, tc in enumerate(tool_calls):
        # Skip already-processed tool calls on resume
        if i < resume_from_idx:
            continue

        result = await process_tool_call(
            conn, tc, session_db_id, project_id, embedding_model_id, dry_run
        )

        if result == "processed":
            processed += 1
        elif result == "skipped":
            skipped += 1
        elif result == "error":
            errors += 1

        # Update progress periodically (including resume index)
        if not dry_run and (i + 1) % batch_size == 0:
            await conn.execute("""
                UPDATE backfill_log SET processed = $2, skipped = $3, errors = $4, last_processed_idx = $5
                WHERE session_id = $1
            """, sid, processed, skipped, errors, i + 1)
            logger.info(f"  Progress: {i+1}/{total} (processed={processed}, skipped={skipped}, errors={errors})")

    # Insert user prompts
    user_prompts = parsed.get("user_prompts", [])
    prompts_inserted = 0
    if not dry_run and user_prompts:
        # Check if prompts already exist for this session (idempotent)
        existing = await conn.fetchval(
            "SELECT count(*) FROM mem_user_prompts WHERE session_id = $1", session_db_id
        )
        if existing == 0:
            for up in user_prompts:
                created_at = datetime.now(timezone.utc)
                if up.get("timestamp"):
                    try:
                        ts = up["timestamp"]
                        if ts.endswith("Z"):
                            ts = ts[:-1] + "+00:00"
                        created_at = datetime.fromisoformat(ts)
                    except (ValueError, TypeError):
                        pass
                await conn.execute("""
                    INSERT INTO mem_user_prompts (session_id, prompt_number, prompt_text, created_at)
                    VALUES ($1, $2, $3, $4)
                """, session_db_id, up["prompt_number"], up["prompt_text"], created_at)
                prompts_inserted += 1
            logger.info(f"  Inserted {prompts_inserted} user prompts")
        else:
            logger.info(f"  User prompts already exist ({existing}), skipping")
    elif dry_run and user_prompts:
        logger.info(f"  [dry-run] Would insert {len(user_prompts)} user prompts")

    # Final update
    if not dry_run:
        await conn.execute("""
            UPDATE backfill_log
            SET processed = $2, skipped = $3, errors = $4,
                last_processed_idx = $5, status = 'done', completed_at = now()
            WHERE session_id = $1
        """, sid, processed, skipped, errors, total)

    logger.info(f"  Done: processed={processed}, skipped={skipped}, errors={errors}, prompts={prompts_inserted}")
    return {"processed": processed, "skipped": skipped, "errors": errors, "total": total, "status": "done"}


# ── Main ──────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Backfill observations from Claude Code JSONL logs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--session", type=str, help="Process a single session UUID")
    parser.add_argument("--jsonl-dir", type=str, default=str(DEFAULT_JSONL_DIR),
                        help=f"Directory to scan for JSONL files (default: {DEFAULT_JSONL_DIR})")
    parser.add_argument("--batch-size", type=int, default=10, help="Progress update frequency")
    parser.add_argument("--reprocess-tools", type=str, nargs="+",
                        help="Re-scan done sessions for specific tool names (e.g. AskUserQuestion ExitPlanMode)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Discover JSONL files
    files = discover_jsonl_files(args.jsonl_dir, args.session)
    if not files:
        logger.error(f"No JSONL files found in {args.jsonl_dir}" +
                      (f" matching session {args.session}" if args.session else ""))
        sys.exit(1)

    logger.info(f"Found {len(files)} JSONL session files ({sum(f['size_kb'] for f in files):.1f} KB total)")

    if args.dry_run:
        # In dry-run, parse all files and show summary
        total_tools = 0
        for f in files:
            parsed = parse_jsonl_session(f["path"])
            tc_count = len(parsed["tool_calls"])
            total_tools += tc_count
            # Get date from first tool call
            first_ts = ""
            if parsed["tool_calls"]:
                first_ts = (parsed["tool_calls"][0].get("timestamp") or "")[:10]
            print(f"  {f['session_id'][:12]}...  {first_ts:>12}  {f['size_kb']:>8.1f}KB  {tc_count:>4} tool calls")
        print(f"\nTotal: {total_tools} tool calls across {len(files)} sessions")

        # Also do a sample run through LLM for first few
        if total_tools > 0:
            conn = await get_connection()
            print("\nSample LLM output (first 3 non-skip tool calls):")
            sample_count = 0
            for f in files:
                if sample_count >= 3:
                    break
                parsed = parse_jsonl_session(f["path"])
                for tc in parsed["tool_calls"]:
                    if tc["tool_name"] in SKIP_TOOLS:
                        continue
                    result = await process_tool_call(conn, tc, 0, 0, None, dry_run=True)
                    sample_count += 1
                    if sample_count >= 3:
                        break
            await conn.close()
        return

    # Reprocess mode: scan done sessions for specific tools only
    if args.reprocess_tools:
        conn = await get_connection()
        target_tools = set(args.reprocess_tools)
        logger.info(f"Reprocess mode: looking for {target_tools} in completed sessions")

        embedding_model_id = await get_embedding_model_id(conn)
        totals = {"processed": 0, "skipped": 0, "errors": 0, "total": 0}

        for f in files:
            sid = f["session_id"]
            parsed = parse_jsonl_session(f["path"])

            # Filter to only target tools
            target_calls = [tc for tc in parsed["tool_calls"] if tc["tool_name"] in target_tools]
            if not target_calls:
                continue

            logger.info(f"Session {sid[:12]}...: {len(target_calls)} {'/'.join(target_tools)} calls")
            totals["total"] += len(target_calls)

            # Get cwd from first call with one
            first_cwd = None
            for tc in parsed["tool_calls"]:
                if tc.get("cwd"):
                    first_cwd = tc["cwd"]
                    break

            session_db_id = await get_or_create_session(conn, sid, first_cwd, target_calls[0].get("timestamp"))
            sess_row = await conn.fetchrow("SELECT project_id FROM mem_sessions WHERE id = $1", session_db_id)
            project_id = sess_row["project_id"]

            for tc in target_calls:
                result = await process_tool_call(conn, tc, session_db_id, project_id, embedding_model_id)
                if result == "processed":
                    totals["processed"] += 1
                elif result == "skipped":
                    totals["skipped"] += 1
                elif result == "error":
                    totals["errors"] += 1

        await conn.close()
        print(f"\nReprocess complete:")
        print(f"  Target tools: {', '.join(args.reprocess_tools)}")
        print(f"  Total calls found: {totals['total']}")
        print(f"  Processed: {totals['processed']}")
        print(f"  Skipped (LLM): {totals['skipped']}")
        print(f"  Errors: {totals['errors']}")
        return

    # Real run
    conn = await get_connection()

    totals = {"processed": 0, "skipped": 0, "errors": 0, "total": 0}
    for f in files:
        result = await process_session(conn, f, dry_run=False, batch_size=args.batch_size)
        for k in totals:
            totals[k] += result.get(k, 0)

    await conn.close()

    print(f"\nBackfill complete:")
    print(f"  Sessions: {len(files)}")
    print(f"  Total tool calls: {totals['total']}")
    print(f"  Processed: {totals['processed']}")
    print(f"  Skipped: {totals['skipped']}")
    print(f"  Errors: {totals['errors']}")


if __name__ == "__main__":
    asyncio.run(main())
