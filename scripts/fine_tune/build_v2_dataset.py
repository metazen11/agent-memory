#!/usr/bin/env python3
"""Build the v2 fine-tune dataset from agent-memory's linked tool_calls.

Reads (mem_user_prompts, mem_tool_calls, mem_sessions) joined via the
prev_user_prompt_id linkage put in place by migration 012 + backfill (#28).
Emits Qwen 2.5 chat-template-compatible rows to
``data/processed/qwen25_tools/v2/``.

Key v2 vs v1 differences
------------------------
* **Real user prompts.** Each row's user message is the actual prompt that
  preceded the tool call, not the v1 synthetic
  "Call tool ``X`` with appropriate arguments."
* **Tool schemas with descriptions** (Step 7 follow-up).
* **Bash sub-classification.** Bash rows record the first non-flag command
  token (``git``, ``pytest``, ``psql``, …) in ``bash_command`` so the row
  cap can be applied per-sub-command rather than per-tool.
* **Filters out v1-style loop-bug shapes.** Empty ``arguments`` are
  dropped unless the tool's schema permits empty args.

Output layout
-------------
    data/processed/qwen25_tools/v2/
        train.chat.jsonl       full train split
        valid.chat.jsonl       full valid split (5%)
        train.tiny.jsonl       200-row deterministic sample (seed=42)
        valid.tiny.jsonl       30-row deterministic sample
        tool_schemas.json      schemas keyed by tool name
        MANIFEST.json          row counts, hashes, filters applied

Run
---
    .venv-finetune/bin/python scripts/fine_tune/build_v2_dataset.py
        — dry-run: prints counts that WOULD be written.

    .venv-finetune/bin/python scripts/fine_tune/build_v2_dataset.py --write
        — writes the output files.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo root on sys.path so `from app...` works when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent \
    / "data" / "processed" / "qwen25_tools" / "v2"

SYSTEM_PROMPT = (
    "You are Qwen, created by Alibaba Cloud. You are a helpful assistant "
    "with access to tools."
)

V1_TOOL_SCHEMAS = (
    Path(__file__).resolve().parent.parent.parent
    / "data" / "processed" / "qwen25_tools" / "v1" / "tool_schemas.json"
)

# Cap each tool (or Bash sub-command) at this fraction of total rows so no
# single category dominates. Tightened later if histogram comes back skewed.
PER_TOOL_CAP_FRACTION = 0.20
MIN_TURNS_PER_SESSION = 2
VALID_SPLIT_FRACTION = 0.05
TINY_TRAIN_ROWS = 200
TINY_VALID_ROWS = 30
RANDOM_SEED = 42


# ── SQL ───────────────────────────────────────────────

_SELECT_LINKED_ROWS_SQL = """
SELECT
    tc.id                       AS tool_call_id,
    tc.tool_name,
    tc.tool_input,
    tc.tool_response_preview,
    tc.turn_index,
    tc.turn_subindex,
    tc.prev_user_prompt_id,
    tc.session_id               AS session_db_id,
    s.session_id                AS session_uuid,
    up.prompt_text,
    up.prompt_number,
    p.canonical_root_path,
    p.git_remote,
    p.source_kind
FROM mem_tool_calls tc
JOIN mem_user_prompts up ON up.id = tc.prev_user_prompt_id
JOIN mem_sessions     s  ON s.id = tc.session_id
JOIN mem_projects     p  ON p.id = tc.project_id
WHERE tc.retention_class = 'backfill_jsonl'
  AND p.source_kind != 'ephemeral'
ORDER BY tc.session_id, tc.turn_index, tc.turn_subindex, tc.id
"""


# ── Helpers ───────────────────────────────────────────

# Tools we never want in training data (UI / planning / no real
# observable side-effect).
SKIP_TOOLS = {
    "AskUserQuestion", "TodoWrite", "TaskCreate", "TaskUpdate",
    "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "EnterPlanMode", "ExitPlanMode", "ListMcpResourcesTool",
    "SlashCommand", "Skill",
}


# Recovered schemas — tools we observed in the backfilled corpus that v1
# never had a schema for. Properties are permissive (the model learns
# argument shape from examples). Keys are derived from sampled
# tool_input shapes in the live DB.
_RECOVERED_SCHEMAS = {
    "Agent": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "subagent_type": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["prompt"],
    },
    # Asana MCP tools — observed in the corpus, used for project tracking.
    "mcp__asana__asana_update_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "completed": {"type": "boolean"},
            "html_notes": {"type": "string"},
            "name": {"type": "string"},
            "due_on": {"type": "string"},
            "assignee": {"type": "string"},
        },
        "required": ["task_id"],
    },
    "mcp__asana__asana_get_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "opt_fields": {"type": "string"},
        },
        "required": ["task_id"],
    },
    "mcp__asana__asana_create_task": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "notes": {"type": "string"},
            "projects": {"type": "array"},
            "workspace": {"type": "string"},
            "assignee": {"type": "string"},
        },
        "required": ["name"],
    },
    "mcp__asana__asana_create_task_story": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["task_id", "text"],
    },
    "mcp__asana__asana_typeahead_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "resource_type": {"type": "string"},
        },
        "required": ["query"],
    },
    "mcp__asana__asana_get_stories_for_task": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
    },
    # agent-memory MCP tools.
    "mcp__agent-memory__save_memory": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "title": {"type": "string"},
            "project": {"type": "string"},
        },
        "required": ["text"],
    },
    "mcp__agent-memory__search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "project": {"type": "string"},
        },
        "required": ["query"],
    },
    "mcp__agent-memory__create_lesson": {
        "type": "object",
        "properties": {
            "rule": {"type": "string"},
            "title": {"type": "string"},
            "severity": {"type": "string"},
            "trigger_on": {"type": "string"},
            "trigger_pattern": {"type": "string"},
            "project": {"type": "string"},
        },
        "required": ["rule"],
    },
    "mcp__agent-memory__get_observations": {
        "type": "object",
        "properties": {
            "ids": {"type": "array"},
        },
        "required": ["ids"],
    },
    # Anvil ghissue helper.
    "mcp__anvil__ghissue_create": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "repo": {"type": "string"},
        },
        "required": ["title"],
    },
    # Monitor (long-running watcher).
    "Monitor": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "description": {"type": "string"},
            "timeout_ms": {"type": "integer"},
            "persistent": {"type": "boolean"},
        },
        "required": ["command", "description"],
    },
}

_RECOVERED_DESCRIPTIONS = {
    "Agent": "Spawn a sub-agent task with a prompt.",
    "mcp__asana__asana_update_task": "Update an Asana task (status, notes, completion).",
    "mcp__asana__asana_get_task": "Fetch an Asana task by id.",
    "mcp__asana__asana_create_task": "Create a new Asana task.",
    "mcp__asana__asana_create_task_story": "Comment on an Asana task.",
    "mcp__asana__asana_typeahead_search": "Search Asana for tasks/projects by name.",
    "mcp__asana__asana_get_stories_for_task": "List comments on an Asana task.",
    "mcp__agent-memory__save_memory": "Save an observation to agent-memory.",
    "mcp__agent-memory__search": "Semantic search over agent-memory observations.",
    "mcp__agent-memory__create_lesson": "Create a proactive rule (lesson) in agent-memory.",
    "mcp__agent-memory__get_observations": "Fetch full details for a list of observation ids.",
    "mcp__anvil__ghissue_create": "Create a GitHub issue via Anvil.",
    "Monitor": "Start a long-running background watcher that streams events.",
}


def _load_tool_schemas() -> dict[str, dict]:
    """Reuse v1's schema registry + recovered v2 schemas for top backfill tools."""
    schemas: dict[str, dict] = {}
    if V1_TOOL_SCHEMAS.exists():
        with V1_TOOL_SCHEMAS.open() as f:
            schemas.update(json.load(f))
    else:
        logger.warning("v1 tool_schemas.json not found")
    schemas.update(_RECOVERED_SCHEMAS)
    return schemas


# Concise one-line descriptions per tool. Kept short — long descriptions
# would bloat the prompt and tax the model's attention budget.
TOOL_DESCRIPTIONS = {
    "Bash": "Run a shell command. Use for git, build tools, test runners.",
    "Edit": "Edit a file by replacing one string with another.",
    "Glob": "Find files by glob pattern.",
    "Grep": "Search file contents with regex.",
    "Read": "Read a file from disk.",
    "WebFetch": "Fetch a URL and run a prompt over its contents.",
    "WebSearch": "Search the web.",
    "Write": "Write text to a file (creates parent dirs).",
    "analyze_image": "Analyze an image file.",
    "anvil_task_complete": "Mark a task complete in the Anvil pipeline.",
    "bash_run": "Run a shell command (Anvil tool).",
    "edit_file": "Edit a file (Anvil tool, like Edit).",
    "extend_help": "Get help on extending Anvil.",
    "fetch_url": "Fetch a URL (Anvil tool, like WebFetch).",
    "glob_search": "Glob files (Anvil tool, like Glob).",
    "grep_search": "Search file contents (Anvil tool, like Grep).",
    "list_files": "List files and metadata (Anvil tool).",
    "read_file": "Read a file (Anvil tool, like Read).",
    "search_session_history": "Search agent-memory for prior observations.",
    "test_tool": "Test fixture — not a real tool.",
    "web_browser": "Headless browser fetch with JS rendering.",
    "write_file": "Write a file (Anvil tool, like Write).",
    **_RECOVERED_DESCRIPTIONS,
}


_BASH_FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)")


def _bash_command_token(command: str | None) -> str:
    """Return the first non-flag token of a Bash command.

    Strips leading shell prefixes (``cd /dir && ...``, ``env VAR=val ...``,
    ``VAR=val ...``) so the real command (``git``, ``pytest``) is captured.
    """
    if not command:
        return "unknown"
    # Strip leading `cd ... && ` prefixes.
    cmd = re.sub(r"^cd\s+\S+\s*&&\s*", "", command, count=1)
    # Strip leading `env ` keyword if present.
    cmd = re.sub(r"^env\s+", "", cmd)
    # Strip `VAR=val ` prefixes (bare and post-env).
    cmd = re.sub(r"^(?:[A-Za-z_]\w*=\S+\s+)+", "", cmd)
    m = _BASH_FIRST_TOKEN_RE.match(cmd)
    return m.group(1).lower() if m else "unknown"


def _build_tool_envelope(name: str, schemas: dict[str, dict]) -> dict | None:
    """Wrap a JSON Schema in the OpenAI-style function-tool envelope.

    Returns None if we don't have a schema for the tool (we skip those rows
    so the model never sees an unschematized tool).
    """
    schema = schemas.get(name)
    if not schema:
        return None
    parameters = {
        "type": "object",
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": TOOL_DESCRIPTIONS.get(
                name, f"Tool '{name}' (description inferred from training data)."
            ),
            "parameters": parameters,
        },
    }


def _parse_tool_input(raw: Any) -> dict[str, Any]:
    """tool_input is stored as a JSON column — asyncpg gives us a string."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _is_empty_args_problematic(name: str, args: dict, schemas: dict[str, dict]) -> bool:
    """The v1 loop-bug shape: empty args when schema requires fields."""
    if args:
        return False
    schema = schemas.get(name)
    if not schema:
        # No schema means we'll drop the row anyway; don't double-flag.
        return False
    return bool(schema.get("required"))


def _make_row(rec: asyncpg.Record, schemas: dict[str, dict]) -> dict | None:
    """Convert one DB row into the Qwen 2.5 chat-template shape.

    Returns None if the row should be dropped (missing schema, empty args
    against a non-empty schema, skip-listed tool, etc.).
    """
    tool_name = rec["tool_name"]
    if not tool_name or tool_name in SKIP_TOOLS:
        return None

    args = _parse_tool_input(rec["tool_input"])
    if _is_empty_args_problematic(tool_name, args, schemas):
        return None

    envelope = _build_tool_envelope(tool_name, schemas)
    if envelope is None:
        return None

    prompt_text = (rec["prompt_text"] or "").strip()
    if not prompt_text:
        return None

    response = (rec["tool_response_preview"] or "").strip()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": prompt_text},
        {"role": "assistant", "tool_calls": [{
            "type": "function",
            "function": {"name": tool_name, "arguments": args},
        }]},
        {"role": "tool", "name": tool_name, "content": response},
    ]

    row: dict[str, Any] = {
        "messages": messages,
        "tools": [envelope],
        "source": "claude_jsonl",
        "session_id": rec["session_uuid"],
        "synthetic": False,
    }
    if tool_name in ("Bash", "bash_run"):
        cmd = args.get("command") if isinstance(args, dict) else None
        row["bash_command"] = _bash_command_token(cmd)
    return row


def _apply_caps(rows: list[dict]) -> list[dict]:
    """Cap each tool (and each Bash sub-command) at PER_TOOL_CAP_FRACTION.

    Random stratified sample within each over-cap category.
    """
    if not rows:
        return rows
    total = len(rows)
    cap = max(1, int(total * PER_TOOL_CAP_FRACTION))

    grouped: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        # Group key: tool_name for non-bash, "Bash:<command>" for bash.
        first_tool = r["messages"][2]["tool_calls"][0]["function"]["name"]
        if first_tool in ("Bash", "bash_run") and r.get("bash_command"):
            key = f"{first_tool}:{r['bash_command']}"
        else:
            key = first_tool
        grouped.setdefault(key, []).append(i)

    rng = random.Random(RANDOM_SEED)
    keep_indices: set[int] = set()
    for key, idxs in grouped.items():
        if len(idxs) <= cap:
            keep_indices.update(idxs)
        else:
            keep_indices.update(rng.sample(idxs, cap))

    return [rows[i] for i in sorted(keep_indices)]


def _filter_sessions_by_min_turns(rows: list[dict]) -> list[dict]:
    """Drop rows from sessions with fewer than MIN_TURNS_PER_SESSION turns."""
    by_session: Counter[str] = Counter(r["session_id"] for r in rows)
    return [r for r in rows if by_session[r["session_id"]] >= MIN_TURNS_PER_SESSION]


def _stable_hash(rows: list[dict]) -> str:
    """SHA256 of newline-separated canonical JSON. Used for MANIFEST."""
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def _split_train_valid(
    rows: list[dict], rng: random.Random
) -> tuple[list[dict], list[dict]]:
    """Session-aware split: a session goes entirely to train OR entirely to valid."""
    sessions = sorted({r["session_id"] for r in rows})
    rng.shuffle(sessions)
    n_valid = max(1, int(len(sessions) * VALID_SPLIT_FRACTION))
    valid_sessions = set(sessions[:n_valid])
    train = [r for r in rows if r["session_id"] not in valid_sessions]
    valid = [r for r in rows if r["session_id"] in valid_sessions]
    return train, valid


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _print_histogram(rows: list[dict], label: str) -> dict[str, int]:
    hist: Counter[str] = Counter()
    for r in rows:
        tool = r["messages"][2]["tool_calls"][0]["function"]["name"]
        if tool in ("Bash", "bash_run") and r.get("bash_command"):
            hist[f"{tool}:{r['bash_command']}"] += 1
        else:
            hist[tool] += 1
    print(f"\n--- {label} (top 20) ---")
    for key, n in hist.most_common(20):
        print(f"  {key:40s} {n:>6d}")
    print(f"  (total categories: {len(hist)})")
    return dict(hist)


# ── Driver ────────────────────────────────────────────

async def _fetch_rows(dsn: str) -> list[asyncpg.Record]:
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgres://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(_SELECT_LINKED_ROWS_SQL)
    finally:
        await conn.close()


async def run(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    schemas = _load_tool_schemas()
    logger.info(f"Loaded {len(schemas)} tool schemas from v1.")

    logger.info("Fetching linked rows from agent-memory DB...")
    raw_rows = await _fetch_rows(settings.effective_database_url)
    logger.info(f"Fetched {len(raw_rows)} candidate rows.")

    drop_reasons: Counter[str] = Counter()
    converted: list[dict] = []
    for rec in raw_rows:
        row = _make_row(rec, schemas)
        if row is None:
            # Capture rough reason for the manifest.
            tn = rec["tool_name"] or "<none>"
            if not tn or tn in SKIP_TOOLS:
                drop_reasons["skip_tool"] += 1
            elif tn not in schemas:
                drop_reasons["missing_schema"] += 1
            elif not (rec["prompt_text"] or "").strip():
                drop_reasons["empty_prompt"] += 1
            else:
                drop_reasons["empty_args_with_required"] += 1
            continue
        converted.append(row)

    logger.info(f"Converted {len(converted)} rows (dropped {sum(drop_reasons.values())}).")
    print("\n--- Drop reasons ---")
    for reason, n in drop_reasons.most_common():
        print(f"  {reason:30s} {n:>6d}")

    short_session = _filter_sessions_by_min_turns(converted)
    dropped_short = len(converted) - len(short_session)
    logger.info(f"After short-session filter (< {MIN_TURNS_PER_SESSION} turns): "
                f"{len(short_session)} rows (dropped {dropped_short}).")

    capped = _apply_caps(short_session)
    dropped_cap = len(short_session) - len(capped)
    logger.info(f"After per-tool cap ({int(PER_TOOL_CAP_FRACTION*100)}% per category): "
                f"{len(capped)} rows (dropped {dropped_cap}).")

    hist = _print_histogram(capped, "Final tool distribution")

    if not args.write:
        print("\n=== DRY-RUN: no files written. Re-run with --write to produce v2/. ===")
        return 0

    rng = random.Random(RANDOM_SEED)
    train, valid = _split_train_valid(capped, rng)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUTPUT_DIR / "train.chat.jsonl", train)
    _write_jsonl(OUTPUT_DIR / "valid.chat.jsonl", valid)
    # Tiny deterministic samples.
    rng2 = random.Random(RANDOM_SEED)
    tiny_train = rng2.sample(train, min(TINY_TRAIN_ROWS, len(train)))
    tiny_valid = rng2.sample(valid, min(TINY_VALID_ROWS, len(valid)))
    _write_jsonl(OUTPUT_DIR / "train.tiny.jsonl", tiny_train)
    _write_jsonl(OUTPUT_DIR / "valid.tiny.jsonl", tiny_valid)

    # Tool schemas (with descriptions baked in).
    schemas_with_desc = {
        name: {
            **schema,
            "description": TOOL_DESCRIPTIONS.get(
                name, f"Tool '{name}'."
            ),
        }
        for name, schema in schemas.items()
    }
    with (OUTPUT_DIR / "tool_schemas.json").open("w") as f:
        json.dump(schemas_with_desc, f, indent=2)

    manifest = {
        "build_utc": datetime.now(timezone.utc).isoformat(),
        "source": "agent-memory backfill_jsonl + live capture (retention_class='backfill_jsonl')",
        "row_counts": {
            "fetched":                len(raw_rows),
            "converted":              len(converted),
            "after_short_filter":     len(short_session),
            "after_cap":              len(capped),
            "train":                  len(train),
            "valid":                  len(valid),
            "tiny_train":             len(tiny_train),
            "tiny_valid":             len(tiny_valid),
        },
        "drop_reasons":   dict(drop_reasons),
        "tool_histogram": hist,
        "filters": {
            "skip_tools":              sorted(SKIP_TOOLS),
            "min_turns_per_session":   MIN_TURNS_PER_SESSION,
            "per_tool_cap_fraction":   PER_TOOL_CAP_FRACTION,
            "valid_split_fraction":    VALID_SPLIT_FRACTION,
            "random_seed":             RANDOM_SEED,
        },
        "output_sha256": {
            "train.chat.jsonl":  _stable_hash(train),
            "valid.chat.jsonl":  _stable_hash(valid),
            "train.tiny.jsonl":  _stable_hash(tiny_train),
            "valid.tiny.jsonl":  _stable_hash(tiny_valid),
        },
    }
    with (OUTPUT_DIR / "MANIFEST.json").open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n=== Wrote {OUTPUT_DIR} ===")
    for name, n in manifest["row_counts"].items():
        print(f"  {name:24s} {n:>6d}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true",
                   help="Write output files (default: dry-run with stats).")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
