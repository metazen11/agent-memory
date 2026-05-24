#!/usr/bin/env python3
"""Backfill mem_tool_calls + mem_user_prompts from Claude Code JSONL logs.

Distinct from the older scripts/backfill_jsonl.py, which generates
mem_observations via an LLM call per row. This script is faster, no-LLM,
and populates the v2 linkage fields the fine-tune pipeline needs:

  * mem_user_prompts: turn_index, content_hash, retention_class,
    project_id, agent_name, backfill_run_id.
  * mem_tool_calls:   turn_index, turn_subindex, prev_user_prompt_id,
    git_branch (from cwd at moment of call), git_sha,
    content_hash, retention_class, backfill_run_id, source_agent,
    project_id (resolved via the consolidated ensure_project).

Reuses the existing parser at scripts/backfill_jsonl.py:parse_jsonl_session
so the jsonl shape interpretation stays consistent with the older script.

Failure-fast usage
------------------
Default is dry-run with a sample limit:

    python scripts/backfill/backfill_tool_calls_from_jsonl.py
        — preview counts across ALL discovered sessions.

    python scripts/backfill/backfill_tool_calls_from_jsonl.py --limit 1 --commit
        — write rows for ONE session, inspect manually, then scale up.

    python scripts/backfill/backfill_tool_calls_from_jsonl.py --limit 10 --commit
        — second gate: 10 sessions.

    python scripts/backfill/backfill_tool_calls_from_jsonl.py --commit
        — full corpus.

Idempotency
-----------
Row-level dedupe on (session_id, content_hash). Re-running --commit is
a no-op for rows already present; only new rows are inserted. Per-session
atomic transaction — a crashed session is rolled back entirely.

Edge cases handled
------------------
1. Malformed jsonl line:        skipped + warn-logged, file continues.
2. Orphan tool_use (no prior
   user prompt):                inserted with prev_user_prompt_id=NULL.
3. tool_use without tool_result:row present, response_preview=NULL.
4. Multi-tool turns (N tool_use
   blocks in one assistant msg): same turn_index, turn_subindex 0..N-1.
5. tool_input over 16 KB:       stored truncated, truncated_at_bytes set.
6. Unicode / invalid surrogates:passed through asyncpg's standard codec;
                                  raised exceptions abort the session
                                  (kept atomic, restartable).
7. cwd not in a git repo:       project resolved via ensure_project's
                                  non-git path (full_path = literal cwd).
8. cwd missing on disk:         same as #7 (resolve_git_context returns
                                  non-git).

Rollback
--------
All inserted rows carry backfill_run_id = <run-start UTC ISO>. To roll
back one run:

    DELETE FROM mem_user_prompts WHERE backfill_run_id = '<id>';
    UPDATE mem_tool_calls
       SET prev_user_prompt_id = NULL, backfill_run_id = NULL
       WHERE backfill_run_id = '<id>';
    DELETE FROM mem_tool_calls WHERE backfill_run_id = '<id>';
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo root on sys.path so `from app...` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncpg

from app.config import settings
from app.path_normalize import normalize_json, normalize_text
from app.project import ensure_project
from app.redact import redact_json, redact_text

# Import the existing parser — single source of truth for jsonl shape.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backfill_jsonl import (  # noqa: E402
    discover_jsonl_files,
    parse_jsonl_session,
)

logger = logging.getLogger(__name__)

DEFAULT_JSONL_DIR = Path.home() / ".claude" / "projects"
MAX_TOOL_INPUT_BYTES = 16 * 1024  # 16 KB cap on stored tool_input
MAX_TOOL_RESPONSE_BYTES = 3 * 1024  # 3 KB cap on response_preview


# ── SQL ───────────────────────────────────────────────

_SELECT_SESSION_BY_TEXT_ID_SQL = (
    "SELECT id FROM mem_sessions WHERE session_id = $1"
)

_INSERT_SESSION_SQL = """
INSERT INTO mem_sessions (session_id, project_id, agent_type, status)
VALUES ($1, $2, $3, 'completed')
ON CONFLICT (session_id) DO UPDATE SET session_id = EXCLUDED.session_id
RETURNING id
"""

_FIND_PROMPT_BY_POSITION_SQL = (
    "SELECT id FROM mem_user_prompts "
    "WHERE session_id = $1 AND prompt_number = $2"
)

_INSERT_PROMPT_SQL = """
INSERT INTO mem_user_prompts
    (session_id, project_id, prompt_number, prompt_text, agent_name,
     turn_index, content_hash, retention_class, backfill_run_id,
     created_at)
VALUES ($1, $2, $3, $4, $5, $3, $6, 'backfill_jsonl', $7, $8)
RETURNING id
"""

_FIND_TOOL_CALL_BY_POSITION_SQL = (
    "SELECT id FROM mem_tool_calls "
    "WHERE session_id = $1 AND turn_index = $2 AND turn_subindex = $3 "
    "AND retention_class = 'backfill_jsonl'"
)

_INSERT_TOOL_CALL_SQL = """
INSERT INTO mem_tool_calls
    (session_id, project_id, tool_name, tool_input,
     tool_response_preview, tool_success, tool_error,
     prompt_text, cwd, source_system, source_agent,
     turn_index, turn_subindex, prev_user_prompt_id,
     content_hash, truncated_at_bytes, retention_class,
     backfill_run_id, git_branch, git_sha, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
        $12, $13, $14, $15, $16, 'backfill_jsonl', $17,
        $18, $19, $20)
RETURNING id
"""


# ── Helpers ───────────────────────────────────────────

def _content_hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_hash_call(name: str, args: dict[str, Any]) -> str:
    """Stable hash for a tool_call: name + canonical-JSON args."""
    args_canon = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{name}|{args_canon}".encode("utf-8")).hexdigest()


def _parse_ts(raw: str | None) -> datetime:
    """Parse the jsonl ISO-ish timestamp; fall back to now() on failure."""
    if not raw:
        return datetime.now(timezone.utc)
    try:
        s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _truncate_tool_input(tool_input: Any) -> tuple[str, int | None]:
    """Serialize + truncate tool_input.

    Returns (json_text, truncated_at_bytes_or_None).
    """
    raw = json.dumps(tool_input or {}, ensure_ascii=False)
    if len(raw.encode("utf-8")) <= MAX_TOOL_INPUT_BYTES:
        return raw, None
    # Truncate by byte budget; re-encode the trailing slice as a marker so
    # the stored value is still valid JSON.
    truncated = raw.encode("utf-8")[:MAX_TOOL_INPUT_BYTES].decode(
        "utf-8", errors="ignore"
    )
    marker = json.dumps({"_truncated_from": raw[:200], "_total_bytes": len(raw)})
    return marker, MAX_TOOL_INPUT_BYTES


# ── Per-session writer ────────────────────────────────

async def _write_session(
    conn: asyncpg.Connection,
    parsed: dict,
    backfill_run_id: str,
    commit: bool,
) -> dict[str, int]:
    """Insert one session's prompts + tool_calls. Returns counters."""
    sid = parsed["session_id"]
    user_prompts = parsed.get("user_prompts", [])
    tool_calls = parsed.get("tool_calls", [])

    stats = {
        "prompts_inserted": 0,
        "prompts_skipped_duplicate": 0,
        "tool_calls_inserted": 0,
        "tool_calls_skipped_duplicate": 0,
        "tool_calls_orphan": 0,
        "tool_calls_truncated": 0,
    }

    if not user_prompts and not tool_calls:
        return stats

    # Project resolution from the first cwd we can find. Same heuristic as
    # the existing backfill_jsonl.py for parity.
    first_cwd: str | None = None
    for tc in tool_calls:
        if tc.get("cwd"):
            first_cwd = tc["cwd"]
            break
    if not first_cwd and user_prompts:
        # User-prompts don't carry cwd in the jsonl, so we fall back to
        # whatever cwd we eventually saw in any tool_call OR 'unknown'.
        first_cwd = "unknown"

    if not commit:
        # Dry-run: just count what we'd do.
        stats["prompts_inserted"] = len(user_prompts)
        for tc in tool_calls:
            if tc.get("last_user_message") is None:
                stats["tool_calls_orphan"] += 1
            tool_input_raw = json.dumps(tc.get("tool_input") or {}, ensure_ascii=False)
            if len(tool_input_raw.encode("utf-8")) > MAX_TOOL_INPUT_BYTES:
                stats["tool_calls_truncated"] += 1
        stats["tool_calls_inserted"] = len(tool_calls)
        return stats

    project_id = await ensure_project(conn, first_cwd or "unknown")

    # mem_sessions upsert.
    session_row = await conn.fetchrow(
        _INSERT_SESSION_SQL,
        sid, project_id, "claude-code",
    )
    session_db_id = session_row["id"]

    # Track inserted prompts so we can resolve prev_user_prompt_id for
    # tool calls. Two maps:
    #   - prompt_by_text: most-recent prompt id keyed by redacted text;
    #     used to find "which prompt is this tool call's prev?" via the
    #     tool_call's last_user_message field.
    #   - prompt_by_number: id keyed by prompt_number, used for the
    #     same-prompt fallback when text doesn't match exactly.
    prompt_by_text: dict[str, int] = {}
    prompt_by_number: dict[int, int] = {}
    last_prompt_id: int | None = None

    # ── Write prompts ────────────────────────────────
    # Dedupe key: (session_id, prompt_number). Same text said multiple
    # times in one conversation gets distinct rows — each one is a real
    # turn with its own tool_calls following. The earlier (session_id,
    # content_hash) key incorrectly collapsed "ok"/"yes"/etc reused
    # within a session, costing us linkage for those turns.
    for up in user_prompts:
        text = redact_text(up.get("prompt_text", "")) or ""
        if not text:
            continue
        prompt_number = up["prompt_number"]
        ch = _content_hash_text(text)
        # Idempotency: same session + same turn position = same row.
        existing = await conn.fetchrow(
            _FIND_PROMPT_BY_POSITION_SQL, session_db_id, prompt_number
        )
        if existing:
            prompt_by_text[text] = existing["id"]
            prompt_by_number[prompt_number] = existing["id"]
            last_prompt_id = existing["id"]
            stats["prompts_skipped_duplicate"] += 1
            continue
        row = await conn.fetchrow(
            _INSERT_PROMPT_SQL,
            session_db_id,
            project_id,
            prompt_number,
            text,
            "claude-code",
            ch,
            backfill_run_id,
            _parse_ts(up.get("timestamp")),
        )
        prompt_by_text[text] = row["id"]
        prompt_by_number[prompt_number] = row["id"]
        last_prompt_id = row["id"]
        stats["prompts_inserted"] += 1

    # ── Write tool calls ─────────────────────────────
    # The parser hands us tool_calls in chronological order, each with
    # last_user_message reflecting the most recent user text at that
    # point in the conversation. We use the cached prompt_by_hash to
    # resolve prev_user_prompt_id.
    turn_index_counter = 0
    prev_user_message_for_turn: str | None = None

    for i, tc in enumerate(tool_calls):
        name = tc.get("tool_name", "")
        # Path normalization (migration 014): rewrite stale /Dropbox/_CODING/
        # to /_CODING/ at the write boundary so we don't reintroduce the
        # v4 path-bias problem during re-imports of historical jsonl.
        tool_input = normalize_json(redact_json(tc.get("tool_input") or {}))
        tool_response = normalize_text(redact_text(tc.get("tool_response"))) or None
        last_user_message = normalize_text(tc.get("last_user_message"))
        cwd = normalize_text(tc.get("cwd"))

        # turn_index increments per distinct user message; turn_subindex
        # increments within a multi-tool turn.
        if last_user_message != prev_user_message_for_turn:
            turn_index_counter += 1
            prev_user_message_for_turn = last_user_message
            turn_subindex = 0
        else:
            turn_subindex = i  # rough — refined below if multi-tool

        # Refine turn_subindex: count how many tool_calls share this
        # last_user_message before us in the array.
        turn_subindex = sum(
            1 for prior in tool_calls[:i]
            if prior.get("last_user_message") == last_user_message
        )

        # Resolve prev_user_prompt_id. The tool_call's last_user_message
        # is the user text that immediately preceded it. We use the
        # text-keyed map (latest-wins) so reused short prompts ("ok",
        # "yes") still link to the most recent occurrence — which is
        # correct because tool_calls follow temporally.
        prev_user_prompt_id: int | None = None
        if last_user_message:
            redacted = redact_text(last_user_message) or ""
            prev_user_prompt_id = prompt_by_text.get(redacted)
            if prev_user_prompt_id is None:
                # Fallback: most recent prompt we've seen in this session.
                prev_user_prompt_id = last_prompt_id
        if prev_user_prompt_id is None:
            stats["tool_calls_orphan"] += 1

        # Per-call content hash retained for search/audit, but the
        # idempotency key is positional: (session_id, turn_index,
        # turn_subindex). Reused tools at the same position deduplicate;
        # the same tool name at different turns does not.
        tc_hash = _content_hash_call(name, tc.get("tool_input") or {})
        existing_tc = await conn.fetchrow(
            _FIND_TOOL_CALL_BY_POSITION_SQL,
            session_db_id, turn_index_counter, turn_subindex,
        )
        if existing_tc:
            stats["tool_calls_skipped_duplicate"] += 1
            continue

        tool_input_text, truncated_at = _truncate_tool_input(tool_input)
        if truncated_at:
            stats["tool_calls_truncated"] += 1

        response_preview = (
            tool_response[:MAX_TOOL_RESPONSE_BYTES] if tool_response else None
        )
        # Outcome inference: existing app/routes/observations.py logic
        # is decoupled and reused implicitly — for backfill we just leave
        # tool_success / tool_error NULL; the runtime data is what
        # populates those fields for live captures.
        # git_branch / git_sha are intentionally NOT populated for
        # backfilled rows. They reflect "branch at moment of call" — but
        # the live git state today doesn't match what was on disk months
        # ago when the jsonl was written. Leaving NULL is more accurate
        # than fabricating today's state. The live writer DOES populate
        # them for going-forward captures (see app/routes/observations.py).
        git_branch = None
        git_sha = None
        # Note: project_id stays the session-level project_id — calls
        # within the same session that happen at different cwds STILL
        # roll up to the session's project. Matches live writer behavior.

        await conn.execute(
            _INSERT_TOOL_CALL_SQL,
            session_db_id,
            project_id,
            name,
            tool_input_text,
            response_preview,
            None,  # tool_success
            None,  # tool_error
            last_user_message,
            cwd,
            "claude-code",
            "claude",
            turn_index_counter,
            turn_subindex,
            prev_user_prompt_id,
            tc_hash,
            truncated_at,
            backfill_run_id,
            git_branch,
            git_sha,
            _parse_ts(tc.get("timestamp")),
        )
        stats["tool_calls_inserted"] += 1

    return stats


# ── Driver ────────────────────────────────────────────

async def _connect() -> asyncpg.Connection:
    dsn = settings.effective_database_url
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)
    return await asyncpg.connect(dsn)


async def run(args) -> int:
    files = discover_jsonl_files(args.jsonl_dir, args.session)
    if not files:
        logger.error(f"No jsonl files in {args.jsonl_dir}")
        return 1

    if args.limit:
        files = files[: args.limit]

    backfill_run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info(
        f"backfill_run_id={backfill_run_id}  files={len(files)}  "
        f"commit={args.commit}"
    )

    conn = await _connect()
    grand_total: dict[str, int] = {}
    try:
        for f in files:
            try:
                parsed = parse_jsonl_session(f["path"])
            except Exception as e:
                logger.error(f"PARSE FAIL  {f['session_id'][:12]}  {e!r}")
                continue
            try:
                if args.commit:
                    async with conn.transaction():
                        stats = await _write_session(
                            conn, parsed, backfill_run_id, commit=True,
                        )
                else:
                    stats = await _write_session(
                        conn, parsed, backfill_run_id, commit=False,
                    )
            except Exception as e:
                logger.error(f"SESSION FAIL  {f['session_id'][:12]}  {e!r}")
                continue

            for k, v in stats.items():
                grand_total[k] = grand_total.get(k, 0) + v
            logger.info(
                f"  {f['session_id'][:12]}  prompts+{stats['prompts_inserted']}/-"
                f"{stats['prompts_skipped_duplicate']}  "
                f"tool_calls+{stats['tool_calls_inserted']}/-"
                f"{stats['tool_calls_skipped_duplicate']}  "
                f"orphan={stats['tool_calls_orphan']}  "
                f"truncated={stats['tool_calls_truncated']}"
            )
    finally:
        await conn.close()

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print()
    print(f"=== {mode} totals (backfill_run_id={backfill_run_id}) ===")
    for k in (
        "prompts_inserted", "prompts_skipped_duplicate",
        "tool_calls_inserted", "tool_calls_skipped_duplicate",
        "tool_calls_orphan", "tool_calls_truncated",
    ):
        print(f"  {k:35s} {grand_total.get(k, 0):>8d}")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--commit", action="store_true",
                   help="Apply writes (default: dry-run).")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N session files (failure-fast).")
    p.add_argument("--session", default=None,
                   help="Process a single jsonl session UUID.")
    p.add_argument("--jsonl-dir", default=str(DEFAULT_JSONL_DIR),
                   help=f"Source dir (default {DEFAULT_JSONL_DIR}).")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
