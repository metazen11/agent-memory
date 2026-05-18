#!/usr/bin/env python3
"""Build the v4 fine-tune dataset — MULTI-TURN.

v3 trained on 4-message rows (system → user → assistant(tool_call) → tool)
and regressed: the model never saw a `tool → assistant(text)` transition,
so it can't ground its response on what the tool returned.

v4 fixes this by extending each row, when possible, with the assistant
turn that came AFTER the tool_response in the source .jsonl. That
becomes a 5-message training row:

    system → user → assistant(tool_call) → tool(response) → assistant(text)

The trailing assistant text is included in the label mask so loss is
computed on the post-tool-response reasoning — exactly the behavior the
model needs to learn.

Source-data audit (2026-05-17, full ~/.claude/projects/ corpus):
- 2,472 jsonl files, 120,696 tool_responses
- 58,304 USEFUL multi-turn rows (48% have text-only assistant follow-up)

Design
------
Reuses ALL v3 filters / fixes / SQL (imported from build_v3_dataset).
Adds one transform: walk the source .jsonl forward from the captured
tool_call to find the next assistant text turn, and append it.

Conformance with v3's gates:
- Fix #10 zero-label gate still applies (verified per row).
- All v3 filters (subagent, vision, mutation, repetition, etc) still apply.
- Per-tool cap, project oversampling, text-synth oversampling: kept.
  (The text-synth-oversample now overlaps with multi-turn rows by design;
  multi-turn rows ARE the text-synth target, so we no longer duplicate
  them — see _is_text_synth_candidate_v4.)

Output layout (NEW directory — does NOT touch v3)::

    data/processed/qwen3_tools/v4/
        train.chat.jsonl
        valid.chat.jsonl
        train.tiny.jsonl
        valid.tiny.jsonl
        tool_schemas.json
        MANIFEST.json     adds: multi_turn_pct, multi_turn_resolved,
                                multi_turn_failed_lookup, etc.

Run
---
    .venv-finetune/bin/python scripts/fine_tune/build_v4_dataset.py
        — dry-run with multi-turn stats.

    .venv-finetune/bin/python scripts/fine_tune/build_v4_dataset.py --write
        — writes the output files.

Gate
----
After --write, MANIFEST.multi_turn_pct must be ≥ 0.20 (20%). If it's
lower, source-data audit needs revisiting before retrain.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo root on sys.path so `from app...` works when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncpg

from app.config import settings

# Reuse v3's filters / schemas / helpers wholesale.
from scripts.fine_tune.build_v3_dataset import (  # noqa: E402
    MIN_TURNS_PER_SESSION,
    PER_PROJECT_MAX_PCT,
    PER_TOOL_CAP_FRACTION,
    PROJECT_OVERSAMPLE_FACTOR,
    PROJECT_TAGS,
    RANDOM_SEED,
    SKIP_TOOLS,
    TINY_TRAIN_ROWS,
    TINY_VALID_ROWS,
    VALID_SPLIT_FRACTION,
    _SELECT_LINKED_ROWS_SQL,
    _apply_per_tool_caps,
    _filter_sessions_by_min_turns,
    _load_tool_schemas,
    _make_row,
    _oversample_project_tagged,
    _print_histogram,
    _row_has_predicted_tokens,
    _split_train_valid,
    _stable_hash,
    _strip_audit_fields,
    _write_jsonl,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v4"
JSONL_ROOT = Path.home() / ".claude" / "projects"

# Per-file cache so we read each .jsonl at most once.
_JSONL_CACHE: dict[str, list[dict] | None] = {}
_JSONL_PATH_CACHE: dict[str, Path | None] = {}

# Gate: at least this fraction of train rows must be multi-turn.
MIN_MULTI_TURN_PCT = 0.20

# Cap: how much trailing assistant text to retain. The trainer truncates
# at 1024 tokens; budget ~300 chars to leave room for the tool_response.
MAX_ASSISTANT_TEXT_CHARS = 1200


def _find_jsonl(session_uuid: str) -> Path | None:
    """Locate the .jsonl file for a session UUID under ~/.claude/projects/.

    Cached. Returns None if not found (session may have been cleaned up).
    """
    if session_uuid in _JSONL_PATH_CACHE:
        return _JSONL_PATH_CACHE[session_uuid]
    matches = list(JSONL_ROOT.glob(f"*/{session_uuid}.jsonl"))
    p = matches[0] if matches else None
    _JSONL_PATH_CACHE[session_uuid] = p
    return p


def _load_jsonl_conv(session_uuid: str) -> list[dict] | None:
    """Load conversation messages (user + assistant only) from a session.

    Cached. Returns None if file missing or malformed.
    """
    if session_uuid in _JSONL_CACHE:
        return _JSONL_CACHE[session_uuid]
    path = _find_jsonl(session_uuid)
    if path is None:
        _JSONL_CACHE[session_uuid] = None
        return None
    try:
        rows = [json.loads(line) for line in path.open() if line.strip()]
    except Exception:
        _JSONL_CACHE[session_uuid] = None
        return None
    conv = [r for r in rows if r.get("type") in ("user", "assistant")]
    _JSONL_CACHE[session_uuid] = conv
    return conv


def _extract_text(content: Any) -> str:
    """Concatenate text content from a Claude Code message.content list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text", "")
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(parts).strip()


def _extend_to_multi_turn(
    row: dict,
    rec: asyncpg.Record,
    counters: Counter,
) -> dict:
    """If a follow-up assistant text exists, extend row to 5 messages.

    Otherwise return the row unchanged (still a valid single-turn row).
    """
    session_uuid = rec["session_uuid"]
    if not session_uuid or len(session_uuid) != 36:
        counters["multi_turn_no_session_uuid"] += 1
        return row
    conv = _load_jsonl_conv(session_uuid)
    if conv is None:
        counters["multi_turn_jsonl_missing"] += 1
        return row
    # tool_call_id in mem_tool_calls is the DB int id, not the Claude
    # tool_use id, so we can't use it for lookup. Fall back to tool_name +
    # turn position: pick the n-th matching tool_use in the conversation.
    tool_name = rec["tool_name"]
    turn_seq = rec["turn_seq"] or 1
    # Find Nth assistant tool_use of this name in the conv
    matches: list[int] = []
    for i, msg in enumerate(conv):
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == tool_name:
                matches.append(i)
                break
    if turn_seq - 1 >= len(matches):
        counters["multi_turn_no_match"] += 1
        return row

    emit_idx = matches[turn_seq - 1]
    # Walk forward: skip user(tool_result), inspect next assistant
    if emit_idx + 2 >= len(conv):
        counters["multi_turn_end_of_conv"] += 1
        return row
    nxt = conv[emit_idx + 1]
    if nxt.get("type") != "user":
        counters["multi_turn_no_user_tool_result"] += 1
        return row
    cand = conv[emit_idx + 2]
    if cand.get("type") != "assistant":
        counters["multi_turn_no_assistant_follow"] += 1
        return row
    content = cand.get("message", {}).get("content", [])
    if not isinstance(content, list):
        counters["multi_turn_bad_content"] += 1
        return row
    types = {c.get("type") for c in content if isinstance(c, dict)}
    if "tool_use" in types:
        counters["multi_turn_followup_is_tool_use"] += 1
        return row
    if "text" not in types:
        counters["multi_turn_no_text"] += 1
        return row
    text = _extract_text(content)
    if not text or len(text) < 8:
        counters["multi_turn_text_too_short"] += 1
        return row
    if len(text) > MAX_ASSISTANT_TEXT_CHARS:
        text = text[:MAX_ASSISTANT_TEXT_CHARS].rstrip() + "…"

    # Extend row in place
    new_msgs = list(row["messages"])
    new_msgs.append({"role": "assistant", "content": text})
    row = dict(row)
    row["messages"] = new_msgs
    row["multi_turn"] = True
    counters["multi_turn_resolved"] += 1
    return row


def _print_drops(drops: Counter) -> None:
    print("\n--- Drop reasons (per-fix) ---")
    for reason, n in drops.most_common():
        print(f"  {reason:35s} {n:>6d}")


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
    logger.info(f"Loaded {len(schemas)} tool schemas.")

    logger.info("Fetching linked rows from agent-memory DB...")
    raw_rows = await _fetch_rows(settings.effective_database_url)
    logger.info(f"Fetched {len(raw_rows)} candidate rows.")

    drops: Counter = Counter()
    multi_turn_counters: Counter = Counter()
    converted: list[dict] = []
    fix1_scaffold_mods = 0
    for rec in raw_rows:
        row = _make_row(rec, schemas, descriptions, drops)
        if row is None:
            continue
        # Try to extend to 5-message multi-turn before applying Fix #10.
        row = _extend_to_multi_turn(row, rec, multi_turn_counters)
        # Fix #10: drop rows where the assistant span (now possibly
        # including the trailing text) tokenizes to zero non-special
        # tokens. Multi-turn rows are much more likely to pass.
        if not _row_has_predicted_tokens(row):
            drops["fix10_zero_predicted_tokens"] += 1
            continue
        fix1_scaffold_mods += int(row.get("fix1_scaffold_stripped", 0))
        converted.append(row)

    logger.info(f"Converted {len(converted)} rows (dropped {sum(drops.values())}).")
    _print_drops(drops)

    print("\n--- Multi-turn extension stats ---")
    multi_turn_n = sum(1 for r in converted if r.get("multi_turn"))
    print(f"  rows extended to multi-turn (5-msg):  {multi_turn_n}")
    print(f"  rows left as single-turn (4-msg):     {len(converted) - multi_turn_n}")
    print(f"  multi-turn pct of converted:          {100*multi_turn_n/max(len(converted),1):.2f}%")
    print("  failure reasons (kept as single-turn):")
    for reason, n in multi_turn_counters.most_common():
        if reason == "multi_turn_resolved":
            continue
        print(f"    {reason:35s} {n:>6d}")

    short_session = _filter_sessions_by_min_turns(converted)
    dropped_short = len(converted) - len(short_session)
    logger.info(f"After short-session filter: {len(short_session)} rows (dropped {dropped_short}).")

    capped = _apply_per_tool_caps(short_session)
    dropped_cap = len(short_session) - len(capped)
    logger.info(f"After per-tool cap: {len(capped)} rows (dropped {dropped_cap}).")

    hist = _print_histogram(capped, "Final tool distribution (pre-oversample)")

    multi_turn_after_cap = sum(1 for r in capped if r.get("multi_turn"))
    multi_turn_pct_after_cap = multi_turn_after_cap / max(len(capped), 1)
    print(f"\n  multi-turn pct after per-tool cap:    {multi_turn_pct_after_cap:.2%}")
    if multi_turn_pct_after_cap < MIN_MULTI_TURN_PCT:
        logger.warning(
            f"Multi-turn pct {multi_turn_pct_after_cap:.2%} is below "
            f"minimum gate {MIN_MULTI_TURN_PCT:.0%}. v4 may not improve "
            f"tool_response adaptation. Investigate before training."
        )

    if not args.write:
        print("\n=== DRY-RUN: no files written. Re-run with --write to produce v4/. ===")
        return 0

    rng = random.Random(RANDOM_SEED)
    train, valid = _split_train_valid(capped, rng)

    rng_train = random.Random(RANDOM_SEED + 1)
    # Skip text-synth oversampling for v4: multi-turn rows already ARE the
    # text-synth target. Project-tag oversample still applies.
    train, project_natural, project_added = _oversample_project_tagged(train, rng_train)

    rng_train.shuffle(train)

    multi_turn_train = sum(1 for r in train if r.get("multi_turn"))
    project_tagged_count = sum(1 for r in train if r.get("project_tag"))
    per_project_after_over: dict[str, int] = Counter(
        r["project_tag"] for r in train if r.get("project_tag")
    )

    train_final = _strip_audit_fields(train)
    valid_final = _strip_audit_fields(valid)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUTPUT_DIR / "train.chat.jsonl", train_final)
    _write_jsonl(OUTPUT_DIR / "valid.chat.jsonl", valid_final)

    # Tiny tier uses MAX_LENGTH=512 in the trainer (not 1024). Multi-turn
    # rows that pass Fix #10 at 1024 may fail at 512. Filter tiny against
    # the same gate logic but with the shorter limit. Without this filter
    # the trainer assertion fires on the tiny smoke test (caught
    # 2026-05-18 v4-tiny launch: 4/200 rows had zero predicted tokens
    # at MAX_LENGTH=512).
    from scripts.fine_tune.build_v3_dataset import _load_fix10_tokenizer
    _tok_tiny = _load_fix10_tokenizer()
    TINY_MAX_LENGTH = 512
    HDR = "<|im_start|>assistant"
    END_ = "<|im_end|>"

    def _ok_at_512(row: dict) -> bool:
        try:
            text = _tok_tiny.apply_chat_template(
                row["messages"], tools=row.get("tools"),
                tokenize=False, add_generation_prompt=False,
            )
        except Exception:
            return False
        enc = _tok_tiny(text, truncation=True, max_length=TINY_MAX_LENGTH,
                        padding=False, return_offsets_mapping=True)
        offs = enc["offset_mapping"]
        spans = []
        cur = 0
        while True:
            i = text.find(HDR, cur)
            if i < 0:
                break
            cs = i + len(HDR)
            if cs < len(text) and text[cs] == "\n":
                cs += 1
            j = text.find(END_, cs)
            if j < 0:
                j = len(text)
            spans.append((cs, j))
            cur = j + len(END_)
        if not spans:
            return False
        for s, e in offs:
            if s == e:
                continue
            for ss, ee in spans:
                if s >= ss and e <= ee:
                    return True
        return False

    train_ok_512 = [r for r in train_final if _ok_at_512(r)]
    valid_ok_512 = [r for r in valid_final if _ok_at_512(r)]
    rng_tiny = random.Random(RANDOM_SEED)
    tiny_train = rng_tiny.sample(train_ok_512, min(TINY_TRAIN_ROWS, len(train_ok_512)))
    tiny_valid = rng_tiny.sample(valid_ok_512, min(TINY_VALID_ROWS, len(valid_ok_512)))
    _write_jsonl(OUTPUT_DIR / "train.tiny.jsonl", tiny_train)
    _write_jsonl(OUTPUT_DIR / "valid.tiny.jsonl", tiny_valid)
    print(f"  tiny pool: train_ok_at_512={len(train_ok_512)}/{len(train_final)} "
          f"valid_ok_at_512={len(valid_ok_512)}/{len(valid_final)}")

    schemas_with_desc = {
        name: {**schema, "description": descriptions.get(name, f"Tool '{name}'.")}
        for name, schema in schemas.items()
    }
    with (OUTPUT_DIR / "tool_schemas.json").open("w") as f:
        json.dump(schemas_with_desc, f, indent=2)

    manifest = {
        "build_utc": datetime.now(timezone.utc).isoformat(),
        "base_target": "Qwen/Qwen3-4B (v4 multi-turn)",
        "source": "agent-memory DB + ~/.claude/projects/**/*.jsonl",
        "design": (
            "Each row from mem_tool_calls is extended to 5-message multi-turn "
            "(system→user→assistant(tool_call)→tool→assistant(text)) when the "
            "source .jsonl shows an assistant text turn followed the tool_response. "
            "Trailing text counted in label mask; loss is computed on the "
            "post-tool-response reasoning."
        ),
        "row_counts": {
            "fetched":              len(raw_rows),
            "converted":            len(converted),
            "after_short_filter":   len(short_session),
            "after_cap":            len(capped),
            "train_final":          len(train_final),
            "valid_final":          len(valid_final),
            "tiny_train":           len(tiny_train),
            "tiny_valid":           len(tiny_valid),
        },
        "multi_turn_stats": {
            "resolved_in_converted":     multi_turn_n,
            "failed_reasons":            dict(multi_turn_counters),
            "in_train_after_oversample": multi_turn_train,
            "pct_of_train":              round(multi_turn_train / max(len(train_final), 1), 4),
            "min_gate":                  MIN_MULTI_TURN_PCT,
        },
        "drops_per_fix": {
            "fix1_stop_after_tool_call_mods_in_kept_rows": fix1_scaffold_mods,
            "fix4_subagent_jsonl":           drops.get("subagent_jsonl", 0),
            "fix5_off_distribution_mutation": drops.get("off_distribution_mutation", 0),
            "fix7_in_args_repetition_drops": drops.get("in_args_repetition", 0),
            "fix8_vision_row":               drops.get("vision_row", 0),
            "fix9_task_notification":        drops.get("fix9_task_notification", 0),
            "fix10_zero_predicted_tokens":   drops.get("fix10_zero_predicted_tokens", 0),
            "skip_tool":                     drops.get("skip_tool", 0),
            "missing_schema":                drops.get("missing_schema", 0),
            "empty_prompt":                  drops.get("empty_prompt", 0),
            "empty_args_with_required":      drops.get("empty_args_with_required", 0),
        },
        "fix6_project_tagging": {
            "natural_per_project":     project_natural,
            "oversampled_added_total": project_added,
            "final_per_project":       dict(per_project_after_over),
        },
        "project_tagged_pct": round(project_tagged_count / max(1, len(train_final)), 4),
        "tool_histogram_pre_oversample": hist,
        "filters": {
            "skip_tools":              sorted(SKIP_TOOLS),
            "min_turns_per_session":   MIN_TURNS_PER_SESSION,
            "per_tool_cap_fraction":   PER_TOOL_CAP_FRACTION,
            "valid_split_fraction":    VALID_SPLIT_FRACTION,
            "random_seed":             RANDOM_SEED,
            "max_assistant_text_chars": MAX_ASSISTANT_TEXT_CHARS,
            "project_oversample_factor": PROJECT_OVERSAMPLE_FACTOR,
            "per_project_max_pct":     PER_PROJECT_MAX_PCT,
            "project_tags":            {k: list(v) for k, v in PROJECT_TAGS.items()},
        },
        "output_sha256": {
            "train.chat.jsonl":  _stable_hash(train_final),
            "valid.chat.jsonl":  _stable_hash(valid_final),
            "train.tiny.jsonl":  _stable_hash(tiny_train),
            "valid.tiny.jsonl":  _stable_hash(tiny_valid),
        },
        "notes": [
            "v4 vs v3: only structural change is the multi-turn extension. "
            "All v3 filters preserved (subagent, vision, mutation, repetition, "
            "task-notification, Fix #10 zero-label).",
            "Text-synth oversampling REMOVED in v4 — multi-turn rows already "
            "are the text-synth target and don't need 2× duplication.",
            "Multi-turn pct gate: training MAY be launched if pct < 20%, but "
            "expect tool_response adaptation to remain regressed. The whole "
            "point of v4 is exposing the model to tool→assistant transitions.",
        ],
    }
    with (OUTPUT_DIR / "MANIFEST.json").open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n=== Wrote {OUTPUT_DIR} ===")
    for name, n in manifest["row_counts"].items():
        print(f"  {name:24s} {n:>6d}")
    print(f"\n  multi_turn_pct_of_train  {manifest['multi_turn_stats']['pct_of_train']:.2%}")
    print(f"  project_tagged_pct       {manifest['project_tagged_pct']:.2%}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true",
                   help="Write output files (default: dry-run).")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
