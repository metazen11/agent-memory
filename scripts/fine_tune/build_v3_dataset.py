#!/usr/bin/env python3
"""Build the v3 fine-tune dataset (Qwen3-8B target).

Derived from ``build_v2_dataset.py``. The eight v3 fixes from
``docs/fine_tune/V3_PLAN.md`` §5 are implemented here as additional
filters / transforms on the same DB-sourced row stream:

    1. Stop-after-tool_call cut.
       Truncate any assistant text after the first ``</tool_call>``. We
       rebuild rows from ``mem_tool_calls`` (already structured), so the
       only place stray ``</tool_call>``-then-text can appear is *inside*
       a ``tool_input`` string. We assert + scrub that defensively.

    2. Text-synthesis oversampling.
       Detect rows that are the *last* tool_call for their user prompt
       (i.e. the assistant subsequently produced a text answer with no
       further tool_call). Oversample them 2× with a cap at 20% of the
       training set.

    3. (DPO/KTO preference training — DEFERRED to v3.1 per V3_PLAN §11.)

    4. Subagent-transcript filter.
       Drop rows whose source jsonl path matches ``*/subagents/agent-*.jsonl``.
       We join through ``backfill_log`` (session_id → jsonl_path).

    5. Off-distribution action filter.
       For rows where the prompt is about THIS repo (``agent-memory``),
       drop rows whose first tool_call is a *mutation* action
       (``gh issue create``, ``git commit``, ``git push``, ``npm publish``,
       ``pip install``, ``rm -rf``, etc.). Keep discovery actions.

    6. Project-tagged oversampling.
       Tag rows mentioning the user's projects (``agent-memory``,
       ``fire-map``, ``daily-dispatch``, ``anvil``, ``dispatch``,
       ``validator``, ``TDD``, ``QA``). Oversample 2× with a per-project
       cap at 15% of the training set EACH (post-audit rework — see
       v3-dataset-audit.md; the original aggregate 30% cap bound at zero
       because natural tagged share already exceeded 50%). Per-project
       counts go in MANIFEST.

    7. In-args repetition cap.
       Drop rows whose ANY argument value contains > 3 consecutive
       identical lines, OR exceeds 2,000 chars.

    8. Vision-row filter.
       Drop rows referencing images: ``image_url``, ``[VISION]``,
       ``<image>``, or an ``image_path``-like field in args / prompt /
       response.

    9. Task-notification filter (added post-audit).
       Drop rows whose user prompt starts with ``<task-notification>`` —
       Anvil agent-pipeline mid-workflow system messages, off-distribution
       for the intended use case.

Output layout (NEW directory — does NOT touch v2)::

    data/processed/qwen3_tools/v3/
        train.chat.jsonl
        valid.chat.jsonl
        train.tiny.jsonl        200-row deterministic sample (seed=42)
        valid.tiny.jsonl        30-row deterministic sample
        tool_schemas.json       carries v2 schemas + Glob
        MANIFEST.json           drop counts per fix, per-project counts,
                                text_synthesis_pct, project_tagged_pct,
                                in_args_repetition_drops, output hashes

Run
---
    .venv-finetune/bin/python scripts/fine_tune/build_v3_dataset.py
        — dry-run (default): prints counts that WOULD be written.

    .venv-finetune/bin/python scripts/fine_tune/build_v3_dataset.py --write
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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v3"
V2_TOOL_SCHEMAS = REPO_ROOT / "data" / "processed" / "qwen25_tools" / "v2" / "tool_schemas.json"

# Fix #10 — the trainer renders rows with the target tokenizer's chat
# template and then masks every token outside the assistant span to -100.
# Rows whose assistant span tokenizes to zero non-special tokens produce
# NaN loss under CrossEntropyLoss(ignore_index=-100) at batch_size=1.
# Root cause of 2026-05-16 v3 NaN-eval failure (109/1392 valid +
# 840/22890 train rows). The dataset on disk must contain zero such rows.
FIX10_TOKENIZER_BASE = REPO_ROOT / "models" / "base" / "qwen3-4b"
FIX10_MAX_LENGTH = 1024  # must match run_train_lora.py's MAX_LENGTH for `full` tier
FIX10_ASSISTANT_HEADER = "<|im_start|>assistant"
FIX10_IM_END = "<|im_end|>"

SYSTEM_PROMPT = (
    "You are Qwen, created by Alibaba Cloud. You are a helpful assistant "
    "with access to tools."
)

# Per-tool cap (same fairness lever as v2). Keep at 20%.
PER_TOOL_CAP_FRACTION = 0.20
MIN_TURNS_PER_SESSION = 2
VALID_SPLIT_FRACTION = 0.05
TINY_TRAIN_ROWS = 200
TINY_VALID_ROWS = 30
RANDOM_SEED = 42

# v3-specific oversample params.
TEXT_SYNTH_OVERSAMPLE_FACTOR = 2     # 2× sampling
TEXT_SYNTH_MAX_PCT = 0.20            # cap at 20% of train
PROJECT_OVERSAMPLE_FACTOR = 2
# Per-project cap: each project tag may occupy up to this fraction of train.
# (Replaces the aggregate PROJECT_MAX_PCT used in the original v3 build —
#  the aggregate cap bound at 0 because natural tagged share already
#  exceeded 50%; the per-project cap correctly lifts the under-represented
#  tags 2× without inflating fire-map / anvil. See v3-dataset-audit.md.)
PER_PROJECT_MAX_PCT = 0.15

# Fix #7 repetition cap thresholds.
MAX_ARG_VALUE_CHARS = 2_000
MAX_CONSECUTIVE_IDENTICAL_LINES = 3

# Fix #5: off-distribution mutation actions when the prompt is about THIS repo.
# Detect by first-token of Bash command (e.g. `git`, `gh`, `npm`, `rm`, `pip`).
# Within-Bash sub-actions checked via _is_mutation_bash().
_MUTATION_GH_SUBCMDS = {"issue", "pr", "release", "repo", "api"}
_MUTATION_GIT_SUBCMDS = {"commit", "push", "merge", "rebase", "reset", "tag", "checkout", "branch"}
_MUTATION_NPM_SUBCMDS = {"publish", "install", "i"}
_MUTATION_PIP_SUBCMDS = {"install", "uninstall"}
# Mutation tools (anything that writes to disk or remote).
_MUTATION_TOOLS = {
    "Write", "Edit", "write_file", "edit_file",
    "mcp__anvil__ghissue_create",
    "mcp__asana__asana_create_task",
    "mcp__asana__asana_update_task",
    "mcp__asana__asana_create_task_story",
    "mcp__agent-memory__save_memory",
    "mcp__agent-memory__create_lesson",
}

# This repo path tokens. A prompt is "about agent-memory" if it mentions any.
_THIS_REPO_TOKENS = ("agent-memory", "agentmemory", "agent_memory")

# Project tag patterns (fix #6). Lowercase substring match against prompt
# or cwd path.
PROJECT_TAGS = {
    "agent-memory": ("agent-memory", "agentmemory", "agent_memory"),
    "fire-map": ("fire-map", "firemap", "fire map"),
    "daily-dispatch": ("daily-dispatch", "daily_dispatch", "dailydispatch", " dispatch"),
    "anvil": ("anvil",),
    "validator": ("validator",),
    "tdd-qa": ("tdd", " qa ", "/qa/", "qa.", "playwright"),
}

# ── SQL ───────────────────────────────────────────────

# Pull all linked rows + a `is_last_for_prompt` flag (signals fix #2),
# + cwd for project tagging (fix #6), + jsonl_path via backfill_log when
# available (fix #4). LEFT JOIN on backfill_log because coverage is
# partial — we still want the row, we just can't apply the subagent
# filter to it (which is fine; subagent files almost never make it into
# backfill_log anyway based on observed data).
_SELECT_LINKED_ROWS_SQL = """
WITH ranked AS (
    SELECT
        tc.id                       AS tool_call_id,
        tc.tool_name,
        tc.tool_input,
        tc.tool_response_preview,
        tc.turn_index,
        tc.turn_subindex,
        tc.cwd,
        tc.prev_user_prompt_id,
        tc.session_id               AS session_db_id,
        s.session_id                AS session_uuid,
        up.prompt_text,
        up.prompt_number,
        p.canonical_root_path,
        p.git_remote,
        p.source_kind,
        bl.jsonl_path,
        ROW_NUMBER() OVER (
            PARTITION BY tc.prev_user_prompt_id
            ORDER BY tc.turn_index, tc.turn_subindex, tc.id
        ) AS turn_seq,
        COUNT(*) OVER (PARTITION BY tc.prev_user_prompt_id) AS prompt_tc_total
    FROM mem_tool_calls tc
    JOIN mem_user_prompts up ON up.id = tc.prev_user_prompt_id
    JOIN mem_sessions     s  ON s.id = tc.session_id
    JOIN mem_projects     p  ON p.id = tc.project_id
    LEFT JOIN backfill_log bl ON bl.session_id = s.session_id
    WHERE tc.retention_class = 'backfill_jsonl'
      AND p.source_kind != 'ephemeral'
)
SELECT
    tool_call_id, tool_name, tool_input, tool_response_preview,
    turn_index, turn_subindex, cwd, prev_user_prompt_id,
    session_db_id, session_uuid, prompt_text, prompt_number,
    canonical_root_path, git_remote, source_kind, jsonl_path,
    turn_seq, prompt_tc_total,
    (turn_seq = prompt_tc_total) AS is_last_for_prompt
FROM ranked
ORDER BY session_db_id, turn_index, turn_subindex, tool_call_id
"""

# ── Static schemas (carry from v2) ────────────────────

SKIP_TOOLS = {
    "AskUserQuestion", "TodoWrite", "TaskCreate", "TaskUpdate",
    "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "EnterPlanMode", "ExitPlanMode", "ListMcpResourcesTool",
    "SlashCommand", "Skill",
}


def _load_tool_schemas() -> tuple[dict[str, dict], dict[str, str]]:
    """Load v2 tool_schemas.json and ensure Glob is present.

    Returns (schemas_with_desc, descriptions_only). Schemas are the
    raw shape dict (with $schema/title/etc.); descriptions are the
    one-liner used in the OpenAI function envelope.
    """
    if not V2_TOOL_SCHEMAS.exists():
        raise RuntimeError(f"v2 tool_schemas.json not found at {V2_TOOL_SCHEMAS}")
    schemas: dict[str, dict] = json.loads(V2_TOOL_SCHEMAS.read_text())

    # Ensure Glob is in the registry (v2 had it; this is belt-and-braces).
    if "Glob" not in schemas:
        schemas["Glob"] = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "additionalProperties": True,
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
            "title": "Glob",
            "type": "object",
            "description": "Find files by glob pattern.",
        }

    descriptions = {
        name: (schema.get("description") or f"Tool '{name}'.")
        for name, schema in schemas.items()
    }
    return schemas, descriptions


_BASH_FIRST_TOKEN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)")


def _bash_command_token(command: str | None) -> str:
    if not command:
        return "unknown"
    cmd = re.sub(r"^cd\s+\S+\s*&&\s*", "", command, count=1)
    cmd = re.sub(r"^env\s+", "", cmd)
    cmd = re.sub(r"^(?:[A-Za-z_]\w*=\S+\s+)+", "", cmd)
    m = _BASH_FIRST_TOKEN_RE.match(cmd)
    return m.group(1).lower() if m else "unknown"


def _bash_second_token(command: str | None) -> str | None:
    """Second non-flag token (e.g. `git commit` → `commit`, `gh issue create` → `issue`)."""
    if not command:
        return None
    cmd = re.sub(r"^cd\s+\S+\s*&&\s*", "", command, count=1)
    cmd = re.sub(r"^env\s+", "", cmd)
    cmd = re.sub(r"^(?:[A-Za-z_]\w*=\S+\s+)+", "", cmd)
    parts = re.split(r"\s+", cmd.strip(), maxsplit=3)
    if len(parts) < 2:
        return None
    return parts[1].lower()


def _build_tool_envelope(name: str, schemas: dict[str, dict], descriptions: dict[str, str]) -> dict | None:
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
            "description": descriptions.get(name, f"Tool '{name}'."),
            "parameters": parameters,
        },
    }


def _parse_tool_input(raw: Any) -> dict[str, Any]:
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
    if args:
        return False
    schema = schemas.get(name)
    if not schema:
        return False
    return bool(schema.get("required"))


# ── v3 fixes ──────────────────────────────────────────

# Fix #1: stop-after-tool_call cut.
# In our DB-sourced pipeline the assistant turn is constructed from
# tool_input alone; there is no "text after </tool_call>" to strip at
# the row level. The only way stray scaffolding can leak is INSIDE an
# argument string. We scan and strip.
_STOP_AFTER_TOOL_CALL_PATTERNS = (
    re.compile(r"</tool_call>"),
    re.compile(r"<\|im_start\|>"),
    re.compile(r"<\|im_end\|>"),
)


def _strip_chat_scaffolding_inplace(args: dict) -> int:
    """Walk args; if a string value contains chat scaffolding tokens,
    truncate at the first occurrence. Returns count of modifications.
    """
    mods = 0
    for k, v in list(args.items()):
        if isinstance(v, str):
            min_idx = -1
            for pat in _STOP_AFTER_TOOL_CALL_PATTERNS:
                m = pat.search(v)
                if m and (min_idx < 0 or m.start() < min_idx):
                    min_idx = m.start()
            if min_idx >= 0:
                args[k] = v[:min_idx].rstrip()
                mods += 1
    return mods


# Fix #5: mutation detection.
def _is_mutation_first_call(tool_name: str, args: dict) -> bool:
    """Return True if this tool_call is a *mutation* (write to disk,
    publish, git mutating ops, gh issue/pr ops, install).

    Used only when the prompt is about THIS repo (agent-memory) — see
    `_prompt_is_about_this_repo`.
    """
    if tool_name in _MUTATION_TOOLS:
        return True
    if tool_name in ("Bash", "bash_run"):
        cmd = args.get("command", "") if isinstance(args, dict) else ""
        if not isinstance(cmd, str):
            return False
        first = _bash_command_token(cmd)
        second = _bash_second_token(cmd) or ""
        # rm -rf / rm -f
        if first == "rm" and any(f in cmd for f in (" -rf", " -fr", " -r ", " -f ")):
            return True
        # git mutations
        if first == "git" and second in _MUTATION_GIT_SUBCMDS:
            return True
        # gh mutations (gh issue create, gh pr create, gh release create)
        if first == "gh" and second in _MUTATION_GH_SUBCMDS:
            # `gh issue list` / `gh pr view` / `gh api` GET-ish are discovery
            third = ""
            after_second = re.split(r"\s+", cmd.strip(), maxsplit=3)
            if len(after_second) >= 3:
                third = after_second[2].lower()
            if third in ("list", "view", "status", ""):
                return False
            return True
        # npm publish / npm install
        if first in ("npm", "npx") and second in _MUTATION_NPM_SUBCMDS:
            return True
        # pip install / pip uninstall
        if first in ("pip", "pip3", "pip3.11") and second in _MUTATION_PIP_SUBCMDS:
            return True
    return False


def _prompt_is_about_this_repo(prompt_text: str, cwd: str | None) -> bool:
    text = (prompt_text or "").lower()
    cw = (cwd or "").lower()
    if any(tok in text for tok in _THIS_REPO_TOKENS):
        return True
    if "agentmemory" in cw or "agent-memory" in cw or "agent_memory" in cw:
        return True
    return False


# Fix #6: project tagging.
def _project_tag(prompt_text: str, cwd: str | None) -> str | None:
    """Return first matching project tag, or None."""
    text = (prompt_text or "").lower()
    cw = (cwd or "").lower()
    for tag, patterns in PROJECT_TAGS.items():
        for pat in patterns:
            if pat in text or pat in cw:
                return tag
    return None


# Fix #7: in-args repetition cap.
def _arg_value_violates_cap(value: Any) -> tuple[bool, str | None]:
    """Return (violates, reason)."""
    if isinstance(value, str):
        if len(value) > MAX_ARG_VALUE_CHARS:
            return True, "value_too_long"
        # Check for > MAX_CONSECUTIVE_IDENTICAL_LINES identical consecutive lines.
        lines = value.split("\n")
        if len(lines) > MAX_CONSECUTIVE_IDENTICAL_LINES:
            run = 1
            for i in range(1, len(lines)):
                if lines[i] == lines[i - 1] and lines[i].strip():
                    run += 1
                    if run > MAX_CONSECUTIVE_IDENTICAL_LINES:
                        return True, "repeated_lines"
                else:
                    run = 1
        return False, None
    if isinstance(value, (list, tuple)):
        # Recurse into list elements.
        for item in value:
            v, why = _arg_value_violates_cap(item)
            if v:
                return True, why
    if isinstance(value, dict):
        for sub in value.values():
            v, why = _arg_value_violates_cap(sub)
            if v:
                return True, why
    return False, None


def _args_violate_repetition_cap(args: dict) -> tuple[bool, str | None]:
    for v in args.values():
        violated, why = _arg_value_violates_cap(v)
        if violated:
            return True, why
    return False, None


# Fix #8: vision-row detection.
_VISION_MARKERS = ("[vision]", "<image>", "image_url")


def _row_references_image(prompt_text: str, response: str, args: dict) -> bool:
    blob = " ".join([
        (prompt_text or "").lower(),
        (response or "").lower()[:5000],
        json.dumps(args, default=str).lower()[:5000],
    ])
    if any(m in blob for m in _VISION_MARKERS):
        return True
    # image_path / image_paths style keys in args
    if isinstance(args, dict):
        for k in args.keys():
            kl = k.lower()
            if kl in ("image_path", "image_paths", "image", "images"):
                return True
    return False


# Fix #4: subagent transcript filter.
def _is_subagent_jsonl(jsonl_path: str | None) -> bool:
    if not jsonl_path:
        return False
    p = jsonl_path
    # Matches `*/subagents/agent-*.jsonl` (the brief's pattern).
    return "/subagents/agent-" in p


# ── Row builder ───────────────────────────────────────


def _make_row(
    rec: asyncpg.Record,
    schemas: dict[str, dict],
    descriptions: dict[str, str],
    drops: Counter,
) -> dict | None:
    tool_name = rec["tool_name"]
    if not tool_name or tool_name in SKIP_TOOLS:
        drops["skip_tool"] += 1
        return None

    if tool_name not in schemas:
        drops["missing_schema"] += 1
        return None

    args = _parse_tool_input(rec["tool_input"])

    # Fix #1: scrub stray chat scaffolding tokens (defensive).
    fix1_mods = _strip_chat_scaffolding_inplace(args)

    if _is_empty_args_problematic(tool_name, args, schemas):
        drops["empty_args_with_required"] += 1
        return None

    prompt_text = (rec["prompt_text"] or "").strip()
    if not prompt_text:
        drops["empty_prompt"] += 1
        return None

    # Fix #9: drop Anvil agent-pipeline task-notification prompts.
    # These are mid-workflow system messages, not natural user prompts —
    # off-distribution for the intended use case (agentic Claude Code
    # replacement). See docs/training_runs/v3-dataset-audit.md.
    if prompt_text.lstrip().startswith("<task-notification>"):
        drops["fix9_task_notification"] += 1
        return None

    response = (rec["tool_response_preview"] or "").strip()

    # Fix #4: subagent transcript filter.
    if _is_subagent_jsonl(rec["jsonl_path"]):
        drops["subagent_jsonl"] += 1
        return None

    # Fix #8: vision-row filter.
    if _row_references_image(prompt_text, response, args):
        drops["vision_row"] += 1
        return None

    # Fix #5: off-distribution mutation actions for this-repo prompts.
    # Only applies to the FIRST tool_call of a prompt (i.e. turn_seq=1).
    if rec["turn_seq"] == 1 and _prompt_is_about_this_repo(prompt_text, rec["cwd"]):
        if _is_mutation_first_call(tool_name, args):
            drops["off_distribution_mutation"] += 1
            return None

    # Fix #7: in-args repetition cap.
    violates, why = _args_violate_repetition_cap(args)
    if violates:
        drops["in_args_repetition"] += 1
        drops[f"in_args_repetition_reason:{why or 'unknown'}"] += 1
        return None

    envelope = _build_tool_envelope(tool_name, schemas, descriptions)
    if envelope is None:
        drops["missing_schema"] += 1
        return None

    messages = [
        {"role": "system",    "content": SYSTEM_PROMPT},
        {"role": "user",      "content": prompt_text},
        {"role": "assistant", "tool_calls": [{
            "type": "function",
            "function": {"name": tool_name, "arguments": args},
        }]},
        {"role": "tool", "name": tool_name, "content": response},
    ]

    project_tag = _project_tag(prompt_text, rec["cwd"])
    row: dict[str, Any] = {
        "messages": messages,
        "tools": [envelope],
        "source": "claude_jsonl",
        "session_id": rec["session_uuid"],
        "synthetic": False,
        "is_text_synth_candidate": bool(rec["is_last_for_prompt"]),
        "project_tag": project_tag,
        "fix1_scaffold_stripped": fix1_mods,
    }
    if tool_name in ("Bash", "bash_run"):
        cmd = args.get("command") if isinstance(args, dict) else None
        row["bash_command"] = _bash_command_token(cmd)
    return row


# ── Fix #10: assistant-span predicted-tokens gate ─────


def _load_fix10_tokenizer():
    """Lazy-load target tokenizer for the Fix #10 predicted-token gate.

    Imported here (not at module top) so dry-runs that don't write don't
    pay the transformers import cost. Cached on first use.
    """
    if not hasattr(_load_fix10_tokenizer, "_tok"):
        from transformers import AutoTokenizer  # noqa: PLC0415
        if not FIX10_TOKENIZER_BASE.exists():
            raise FileNotFoundError(
                f"Fix #10 gate requires {FIX10_TOKENIZER_BASE} (the target tokenizer). "
                "Run scripts/fine_tune/download_base.py qwen3-4b first."
            )
        tok = AutoTokenizer.from_pretrained(
            str(FIX10_TOKENIZER_BASE),
            use_fast=True,
            local_files_only=True,
            trust_remote_code=False,
        )
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        _load_fix10_tokenizer._tok = tok  # type: ignore[attr-defined]
    return _load_fix10_tokenizer._tok  # type: ignore[attr-defined]


def _row_has_predicted_tokens(row: dict) -> bool:
    """Replicate run_train_lora.py's render_with_assistant_mask exactly.

    Returns True iff at least one token in the row's input_ids has its
    label set (i.e. is NOT masked to -100 by the assistant-only mask).

    Must stay in sync with the masking logic in
    models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py.
    """
    tok = _load_fix10_tokenizer()
    text = tok.apply_chat_template(
        row["messages"], tools=row.get("tools"),
        tokenize=False, add_generation_prompt=False,
    )
    enc = tok(
        text, truncation=True, max_length=FIX10_MAX_LENGTH,
        padding=False, return_offsets_mapping=True,
    )
    offsets = enc["offset_mapping"]

    # Walk every <|im_start|>assistant ... <|im_end|> span
    spans = []
    cursor = 0
    while True:
        i = text.find(FIX10_ASSISTANT_HEADER, cursor)
        if i < 0:
            break
        content_start = i + len(FIX10_ASSISTANT_HEADER)
        if content_start < len(text) and text[content_start] == "\n":
            content_start += 1
        j = text.find(FIX10_IM_END, content_start)
        if j < 0:
            j = len(text)
        spans.append((content_start, j))
        cursor = j + len(FIX10_IM_END)

    if not spans:
        return False
    for s, e in offsets:
        if s == e:  # special token, no characters
            continue
        for span_s, span_e in spans:
            if s >= span_s and e <= span_e:
                return True
    return False


# ── Caps + oversample ────────────────────────────────


def _apply_per_tool_caps(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    total = len(rows)
    cap = max(1, int(total * PER_TOOL_CAP_FRACTION))

    grouped: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
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
    by_session: Counter[str] = Counter(r["session_id"] for r in rows)
    return [r for r in rows if by_session[r["session_id"]] >= MIN_TURNS_PER_SESSION]


def _oversample_text_synth(rows: list[dict], rng: random.Random) -> tuple[list[dict], int, int]:
    """Fix #2: oversample text-synthesis candidates at 2×, cap at
    TEXT_SYNTH_MAX_PCT of final.

    Returns (new_rows, base_count, oversample_added).
    """
    base = [r for r in rows if r.get("is_text_synth_candidate")]
    if not base:
        return rows, 0, 0
    # 2× means add `len(base)` more copies (with selection randomized).
    desired_extra = len(base) * (TEXT_SYNTH_OVERSAMPLE_FACTOR - 1)
    # Cap: total text-synth fraction ≤ TEXT_SYNTH_MAX_PCT of resulting rows.
    # If we add E extras, total text-synth = len(base)+E and total rows = len(rows)+E.
    # We want (len(base)+E) / (len(rows)+E) ≤ p.
    p = TEXT_SYNTH_MAX_PCT
    n = len(rows)
    b = len(base)
    # Solve for max E that satisfies: (b+E) ≤ p*(n+E)
    # b + E ≤ p*n + p*E  →  E(1-p) ≤ p*n - b  →  E ≤ (p*n - b)/(1-p)
    if p * n <= b:
        cap_extra = 0
    else:
        cap_extra = int((p * n - b) / (1 - p))
    add_count = max(0, min(desired_extra, cap_extra))
    if add_count == 0:
        return rows, b, 0
    extras = [dict(r, _oversample_origin="text_synth") for r in rng.choices(base, k=add_count)]
    return rows + extras, b, add_count


def _oversample_project_tagged(rows: list[dict], rng: random.Random) -> tuple[list[dict], dict[str, int], int]:
    """Fix #6 (reworked post-audit): oversample project-tagged rows at 2×,
    with a **per-project** cap at PER_PROJECT_MAX_PCT of train.

    For each project tag:
        target = min(natural * PROJECT_OVERSAMPLE_FACTOR, train_size * cap)
        adds   = max(0, target - natural)
    Each row has at most one project_tag (see _project_tag), so de-dup
    by tag is automatic.

    Returns (new_rows, per_project_natural_counts, total_oversample_added).
    """
    tagged_by_proj: dict[str, list[dict]] = {}
    for r in rows:
        tag = r.get("project_tag")
        if tag:
            tagged_by_proj.setdefault(tag, []).append(r)
    natural: dict[str, int] = {tag: len(pool) for tag, pool in tagged_by_proj.items()}
    if not tagged_by_proj:
        return rows, dict(natural), 0

    train_size = len(rows)
    per_project_cap = int(train_size * PER_PROJECT_MAX_PCT)

    extras: list[dict] = []
    for tag, pool in tagged_by_proj.items():
        n_natural = len(pool)
        target = min(n_natural * PROJECT_OVERSAMPLE_FACTOR, per_project_cap)
        adds = max(0, target - n_natural)
        if adds == 0:
            continue
        extras.extend(
            dict(r, _oversample_origin=f"project_tag:{tag}")
            for r in rng.choices(pool, k=adds)
        )

    if not extras:
        return rows, dict(natural), 0
    return rows + extras, dict(natural), len(extras)


def _stable_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        # Drop the v3 audit fields so the hash is portable.
        r_clean = {k: v for k, v in r.items() if not k.startswith("_") and k not in (
            "is_text_synth_candidate", "project_tag", "fix1_scaffold_stripped",
        )}
        h.update(json.dumps(r_clean, sort_keys=True, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def _split_train_valid(rows: list[dict], rng: random.Random) -> tuple[list[dict], list[dict]]:
    sessions = sorted({r["session_id"] for r in rows})
    rng.shuffle(sessions)
    n_valid = max(1, int(len(sessions) * VALID_SPLIT_FRACTION))
    valid_sessions = set(sessions[:n_valid])
    train = [r for r in rows if r["session_id"] not in valid_sessions]
    valid = [r for r in rows if r["session_id"] in valid_sessions]
    return train, valid


def _strip_audit_fields(rows: list[dict]) -> list[dict]:
    """Strip v3-only audit fields before writing the dataset row."""
    out: list[dict] = []
    audit_keys = {"is_text_synth_candidate", "project_tag",
                  "fix1_scaffold_stripped", "_oversample_origin"}
    for r in rows:
        out.append({k: v for k, v in r.items() if k not in audit_keys})
    return out


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
    schemas, descriptions = _load_tool_schemas()
    logger.info(f"Loaded {len(schemas)} tool schemas (v2 + Glob ensured).")

    logger.info("Fetching linked rows from agent-memory DB...")
    raw_rows = await _fetch_rows(settings.effective_database_url)
    logger.info(f"Fetched {len(raw_rows)} candidate rows.")

    drops: Counter = Counter()
    converted: list[dict] = []
    fix1_scaffold_mods = 0
    for rec in raw_rows:
        row = _make_row(rec, schemas, descriptions, drops)
        if row is None:
            continue
        # Fix #10: drop rows where the assistant span tokenizes to zero
        # non-special tokens (would produce NaN loss in trainer at bs=1).
        if not _row_has_predicted_tokens(row):
            drops["fix10_zero_predicted_tokens"] += 1
            continue
        fix1_scaffold_mods += int(row.get("fix1_scaffold_stripped", 0))
        converted.append(row)

    logger.info(f"Converted {len(converted)} rows (dropped {sum(drops.values())}).")
    print("\n--- Drop reasons (per-fix) ---")
    for reason, n in drops.most_common():
        print(f"  {reason:30s} {n:>6d}")
    print(f"  fix1_scaffold_stripped (kept rows w/ mods)     {fix1_scaffold_mods:>6d}")

    short_session = _filter_sessions_by_min_turns(converted)
    dropped_short = len(converted) - len(short_session)
    logger.info(f"After short-session filter (< {MIN_TURNS_PER_SESSION} turns): "
                f"{len(short_session)} rows (dropped {dropped_short}).")

    capped = _apply_per_tool_caps(short_session)
    dropped_cap = len(short_session) - len(capped)
    logger.info(f"After per-tool cap ({int(PER_TOOL_CAP_FRACTION*100)}%): "
                f"{len(capped)} rows (dropped {dropped_cap}).")

    hist = _print_histogram(capped, "Final tool distribution (pre-oversample)")

    if not args.write:
        print("\n=== DRY-RUN: no files written. Re-run with --write to produce v3/. ===")
        # Still report planned oversample counts for transparency.
        rng = random.Random(RANDOM_SEED)
        post_text, text_base, text_added = _oversample_text_synth(capped, rng)
        _, project_natural, project_added = _oversample_project_tagged(post_text, rng)
        print(f"\nPlanned text-synth oversample:   base={text_base}  add={text_added}")
        print(f"Planned project-tag oversample:  base={sum(project_natural.values())}  add={project_added}")
        for tag, n in project_natural.items():
            print(f"  project={tag:20s} natural={n:>5d}")
        return 0

    # Splits first; oversample only on TRAIN.
    rng = random.Random(RANDOM_SEED)
    train, valid = _split_train_valid(capped, rng)

    rng_train = random.Random(RANDOM_SEED + 1)
    train, text_base, text_added = _oversample_text_synth(train, rng_train)
    train, project_natural, project_added = _oversample_project_tagged(train, rng_train)

    # Shuffle train so the oversample tail isn't all bunched at the end.
    rng_train.shuffle(train)

    # Strip audit fields for the final dataset rows; preserve in train/valid copies
    # for per-fix MANIFEST tally before stripping.
    text_synth_count = sum(1 for r in train if r.get("is_text_synth_candidate"))
    project_tagged_count = sum(1 for r in train if r.get("project_tag"))
    per_project_after_over: dict[str, int] = Counter(
        r["project_tag"] for r in train if r.get("project_tag")
    )

    train_final = _strip_audit_fields(train)
    valid_final = _strip_audit_fields(valid)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUTPUT_DIR / "train.chat.jsonl", train_final)
    _write_jsonl(OUTPUT_DIR / "valid.chat.jsonl", valid_final)

    # Tiny deterministic samples (seed=42 — separate RNG so test sets stable).
    rng_tiny = random.Random(RANDOM_SEED)
    tiny_train = rng_tiny.sample(train_final, min(TINY_TRAIN_ROWS, len(train_final)))
    tiny_valid = rng_tiny.sample(valid_final, min(TINY_VALID_ROWS, len(valid_final)))
    _write_jsonl(OUTPUT_DIR / "train.tiny.jsonl", tiny_train)
    _write_jsonl(OUTPUT_DIR / "valid.tiny.jsonl", tiny_valid)

    # Tool schemas (carry from v2; ensure Glob present; bake descriptions).
    schemas_with_desc = {
        name: {**schema, "description": descriptions.get(name, f"Tool '{name}'.")}
        for name, schema in schemas.items()
    }
    with (OUTPUT_DIR / "tool_schemas.json").open("w") as f:
        json.dump(schemas_with_desc, f, indent=2)

    manifest = {
        "build_utc": datetime.now(timezone.utc).isoformat(),
        "base_target": "Qwen/Qwen3-8B (v3) — also valid for Qwen3-4B smoke",
        "source": "agent-memory backfill_jsonl (retention_class='backfill_jsonl')",
        "row_counts": {
            "fetched":                 len(raw_rows),
            "converted":               len(converted),
            "after_short_filter":      len(short_session),
            "after_cap":               len(capped),
            "train_pre_oversample":    len(capped) - len(valid),
            "train_final":             len(train_final),
            "valid_final":             len(valid_final),
            "tiny_train":              len(tiny_train),
            "tiny_valid":              len(tiny_valid),
        },
        "drops_per_fix": {
            "fix1_stop_after_tool_call_mods_in_kept_rows": fix1_scaffold_mods,
            "fix4_subagent_jsonl":           drops.get("subagent_jsonl", 0),
            "fix5_off_distribution_mutation": drops.get("off_distribution_mutation", 0),
            "fix7_in_args_repetition_drops": drops.get("in_args_repetition", 0),
            "fix8_vision_row":               drops.get("vision_row", 0),
            "fix9_task_notification":        drops.get("fix9_task_notification", 0),
            "skip_tool":                     drops.get("skip_tool", 0),
            "missing_schema":                drops.get("missing_schema", 0),
            "empty_prompt":                  drops.get("empty_prompt", 0),
            "empty_args_with_required":      drops.get("empty_args_with_required", 0),
        },
        "fix2_text_synth": {
            "natural_count_in_train": text_base,
            "oversampled_added":      text_added,
            "final_count_in_train":   text_synth_count,
        },
        "fix6_project_tagging": {
            "natural_per_project":     project_natural,
            "oversampled_added_total": project_added,
            "final_per_project":       dict(per_project_after_over),
        },
        "text_synthesis_pct":      round(text_synth_count / max(1, len(train_final)), 4),
        "project_tagged_pct":      round(project_tagged_count / max(1, len(train_final)), 4),
        "in_args_repetition_drops": drops.get("in_args_repetition", 0),
        "tool_histogram_pre_oversample": hist,
        "filters": {
            "skip_tools":              sorted(SKIP_TOOLS),
            "min_turns_per_session":   MIN_TURNS_PER_SESSION,
            "per_tool_cap_fraction":   PER_TOOL_CAP_FRACTION,
            "valid_split_fraction":    VALID_SPLIT_FRACTION,
            "random_seed":             RANDOM_SEED,
            "text_synth_oversample_factor": TEXT_SYNTH_OVERSAMPLE_FACTOR,
            "text_synth_max_pct":      TEXT_SYNTH_MAX_PCT,
            "project_oversample_factor": PROJECT_OVERSAMPLE_FACTOR,
            "per_project_max_pct":     PER_PROJECT_MAX_PCT,
            "max_arg_value_chars":     MAX_ARG_VALUE_CHARS,
            "max_consecutive_identical_lines": MAX_CONSECUTIVE_IDENTICAL_LINES,
            "this_repo_tokens":        list(_THIS_REPO_TOKENS),
            "project_tags":            {k: list(v) for k, v in PROJECT_TAGS.items()},
        },
        "output_sha256": {
            "train.chat.jsonl":  _stable_hash(train_final),
            "valid.chat.jsonl":  _stable_hash(valid_final),
            "train.tiny.jsonl":  _stable_hash(tiny_train),
            "valid.tiny.jsonl":  _stable_hash(tiny_valid),
        },
        "notes": [
            "Fix #1 (stop-after-tool_call) is structurally satisfied because rows are "
            "built from `mem_tool_calls` (no trailing assistant text exists at row level). "
            "The `fix1_stop_after_tool_call_mods_in_kept_rows` counter tracks defensive "
            "in-argument scaffold-token scrubs.",
            "Fix #3 (DPO/KTO) is DEFERRED to v3.1 per V3_PLAN §11. Not implemented here.",
            "Fix #4 subagent filter relies on `backfill_log.jsonl_path`. Coverage of that "
            "table is partial (only ~72 sessions overlap with the backfill_jsonl tool_calls "
            "set on the live DB). The filter is correct for rows where the path is known; "
            "rows without a path are not flagged. Observed real subagent jsonl drops will be "
            "small or zero — this is data state, not a script bug.",
        ],
    }
    with (OUTPUT_DIR / "MANIFEST.json").open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n=== Wrote {OUTPUT_DIR} ===")
    for name, n in manifest["row_counts"].items():
        print(f"  {name:24s} {n:>6d}")
    print(f"\n  text_synthesis_pct      {manifest['text_synthesis_pct']:.2%}")
    print(f"  project_tagged_pct      {manifest['project_tagged_pct']:.2%}")
    print(f"  in_args_repetition_drops {manifest['in_args_repetition_drops']}")
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
