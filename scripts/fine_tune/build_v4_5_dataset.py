#!/usr/bin/env python3
"""Build the v4.5 fine-tune dataset — v4 + three improvements.

Builds on build_v4_dataset.py (which itself reuses build_v3_dataset.py
filters wholesale). Three additions on top of v4:

  1. Include `retention_class='live'` rows (~58k extra).
     The UserPromptSubmit hook captures tool_calls live but doesn't
     populate `prev_user_prompt_id`. We backfill that linkage at query
     time via LATERAL join (session_id + timestamp). Drops prompts
     shorter than 10 chars (mostly 'y', 'ok', 'yes' acks).

  2. Oversample multi-turn rows 2.5x.
     v4 hit 40% multi-turn pct. Target ≥70% for v4.5 by duplicating
     multi-turn rows 2.5x (replicates the natural distribution shift).

  3. Error-recovery row tagging + 3x oversample.
     For each multi-turn row, examine if the FOLLOWUP assistant text
     starts with a pattern that signals 'the tool didn't return what I
     hoped for' — e.g. starts with 'Let me try', 'Actually', 'Hmm,',
     'The file/path doesn't exist', 'No matches', or contains explicit
     phrases like 'failed', 'error', 'try different'. These rows
     directly demonstrate the regression-fix pattern (model recovered
     from a bad tool_response) and get 3x oversampling.

Expected output (target):
  - ~50-60k train rows total
  - ≥70% multi-turn pct
  - ≥10% error-recovery tagged

Output:
  data/processed/qwen3_tools/v4.5/  (NEW dir, doesn't touch v3 or v4)

Run:
  .venv-finetune/bin/python scripts/fine_tune/build_v4_5_dataset.py
  .venv-finetune/bin/python scripts/fine_tune/build_v4_5_dataset.py --write
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncpg
from app.config import settings

# Reuse v3+v4 helpers wholesale.
from scripts.fine_tune.build_v3_dataset import (  # noqa: E402
    PER_TOOL_CAP_FRACTION,
    RANDOM_SEED,
    TINY_TRAIN_ROWS,
    TINY_VALID_ROWS,
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
from scripts.fine_tune.build_v4_dataset import (  # noqa: E402
    MAX_ASSISTANT_TEXT_CHARS,
    _extend_to_multi_turn,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v4.5"

# v4.5 tuning knobs
MIN_LIVE_PROMPT_LEN = 10              # drop tiny 'y', 'ok' acks
MULTI_TURN_OVERSAMPLE_FACTOR = 3.5    # 35.8% natural -> ~70% post-oversample
ERROR_RECOVERY_OVERSAMPLE_FACTOR = 4.0
MIN_MULTI_TURN_PCT = 0.65             # gate for v4.5

# Error-recovery patterns — case-insensitive substring match against
# the leading 200 chars of the followup assistant text.
ERROR_RECOVERY_PATTERNS = re.compile(
    r"(?ix)"
    r"^(?:let me try|actually|hmm[, ]|on second thought|that didn'?t|"
    r"that did not|wait[, ]|oh[, ]|i see |looks like )|"
    r"\b(?:doesn'?t exist|does not exist|no such (?:file|dir|path)|"
    r"not found|no matches|no results|empty result|nothing found|"
    r"failed to|error[: ]|i'?ll try (?:another|a different)|"
    r"that path is wrong|wrong path|let me check)\b"
)


# Unified SQL — backfill + live, with LATERAL prompt linkage for live rows.
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
        COALESCE(tc.prev_user_prompt_id, link.id) AS prev_user_prompt_id,
        tc.session_id               AS session_db_id,
        s.session_id                AS session_uuid,
        COALESCE(up.prompt_text, link.prompt_text) AS prompt_text,
        up.prompt_number,
        p.canonical_root_path,
        p.git_remote,
        p.source_kind,
        bl.jsonl_path,
        tc.retention_class,
        ROW_NUMBER() OVER (
            PARTITION BY COALESCE(tc.prev_user_prompt_id, link.id)
            ORDER BY tc.turn_index, tc.turn_subindex, tc.id
        ) AS turn_seq,
        COUNT(*) OVER (PARTITION BY COALESCE(tc.prev_user_prompt_id, link.id))
            AS prompt_tc_total
    FROM mem_tool_calls tc
    JOIN mem_sessions     s  ON s.id = tc.session_id
    JOIN mem_projects     p  ON p.id = tc.project_id
    LEFT JOIN mem_user_prompts up ON up.id = tc.prev_user_prompt_id
    -- Backfill: for live rows that lack prev_user_prompt_id, find the most
    -- recent user prompt in the same session before this tool_call.
    LEFT JOIN LATERAL (
        SELECT id, prompt_text
        FROM mem_user_prompts
        WHERE session_id = tc.session_id
          AND created_at <= tc.created_at
        ORDER BY created_at DESC
        LIMIT 1
    ) link ON tc.prev_user_prompt_id IS NULL
    LEFT JOIN backfill_log bl ON bl.session_id = s.session_id
    WHERE tc.retention_class IN ('backfill_jsonl', 'live')
      AND p.source_kind != 'ephemeral'
      AND COALESCE(
          length(up.prompt_text),
          length(link.prompt_text),
          0
      ) >= """ + str(MIN_LIVE_PROMPT_LEN) + """
)
SELECT
    tool_call_id, tool_name, tool_input, tool_response_preview,
    turn_index, turn_subindex, cwd, prev_user_prompt_id,
    session_db_id, session_uuid, prompt_text, prompt_number,
    canonical_root_path, git_remote, source_kind, jsonl_path,
    retention_class,
    turn_seq, prompt_tc_total,
    (turn_seq = prompt_tc_total) AS is_last_for_prompt
FROM ranked
ORDER BY session_db_id, turn_index, turn_subindex, tool_call_id
"""


def _is_error_recovery(row: dict) -> bool:
    """A multi-turn row is 'error-recovery' if the trailing assistant
    text starts with or contains a recovery-pattern phrase."""
    if not row.get("multi_turn"):
        return False
    msgs = row.get("messages", [])
    if len(msgs) < 5 or msgs[-1].get("role") != "assistant":
        return False
    text = msgs[-1].get("content", "")
    if not isinstance(text, str):
        return False
    head = text[:200]
    return bool(ERROR_RECOVERY_PATTERNS.search(head))


def _oversample_by_factor(rows: list[dict], pred, factor: float, rng) -> tuple[list[dict], int]:
    """Multiply rows where pred(row) is True by `factor` (probabilistic
    for non-integer factors). Returns (new_rows, n_added)."""
    int_factor = int(factor)
    frac = factor - int_factor
    additions: list[dict] = []
    for r in rows:
        if not pred(r):
            continue
        # (factor - 1) extra copies — the row itself is already in the
        # input list, we only add duplicates.
        for _ in range(int_factor - 1):
            additions.append(dict(r))
        if frac > 0 and rng.random() < frac:
            additions.append(dict(r))
    return rows + additions, len(additions)


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

    logger.info("Fetching backfill + live rows (with LATERAL linkage)...")
    raw_rows = await _fetch_rows(settings.effective_database_url)
    by_class = Counter(r["retention_class"] for r in raw_rows)
    logger.info(f"Fetched {len(raw_rows)} candidate rows: {dict(by_class)}")

    drops: Counter = Counter()
    multi_turn_counters: Counter = Counter()
    converted: list[dict] = []
    for rec in raw_rows:
        row = _make_row(rec, schemas, descriptions, drops)
        if row is None:
            continue
        # Tag retention_class for analysis
        row["retention_class"] = rec["retention_class"]
        # Extend to multi-turn where possible
        row = _extend_to_multi_turn(row, rec, multi_turn_counters)
        # Fix #10 gate
        if not _row_has_predicted_tokens(row):
            drops["fix10_zero_predicted_tokens"] += 1
            continue
        converted.append(row)

    logger.info(f"Converted {len(converted)} rows (dropped {sum(drops.values())}).")
    print("\n--- Drop reasons ---")
    for r, n in drops.most_common():
        print(f"  {r:35s} {n:>6d}")

    # Multi-turn analysis
    mt_n = sum(1 for r in converted if r.get("multi_turn"))
    er_n = sum(1 for r in converted if _is_error_recovery(r))
    live_n = sum(1 for r in converted if r.get("retention_class") == "live")
    backfill_n = sum(1 for r in converted if r.get("retention_class") == "backfill_jsonl")
    print(f"\n--- Pre-oversample counts ---")
    print(f"  backfill_jsonl rows:    {backfill_n}")
    print(f"  live rows:              {live_n}")
    print(f"  multi-turn rows:        {mt_n} ({100*mt_n/max(len(converted),1):.1f}%)")
    print(f"  error-recovery rows:    {er_n} ({100*er_n/max(len(converted),1):.1f}%)")

    short_session = _filter_sessions_by_min_turns(converted)
    capped = _apply_per_tool_caps(short_session)
    logger.info(f"After short-session + per-tool cap: {len(capped)} rows")

    hist = _print_histogram(capped, "Tool dist (pre-oversample)")

    if not args.write:
        rng_preview = random.Random(RANDOM_SEED + 1)
        # Show what oversample WOULD produce
        post_mt, mt_added = _oversample_by_factor(
            capped, lambda r: r.get("multi_turn"),
            MULTI_TURN_OVERSAMPLE_FACTOR, rng_preview,
        )
        post_er, er_added = _oversample_by_factor(
            post_mt, _is_error_recovery,
            ERROR_RECOVERY_OVERSAMPLE_FACTOR, rng_preview,
        )
        mt_final = sum(1 for r in post_er if r.get("multi_turn"))
        er_final = sum(1 for r in post_er if _is_error_recovery(r))
        print(f"\n--- Planned oversample (DRY-RUN) ---")
        print(f"  base rows:                 {len(capped)}")
        print(f"  multi-turn added:          +{mt_added}")
        print(f"  error-recovery added:      +{er_added}")
        print(f"  post-oversample total:     {len(post_er)}")
        print(f"  post-oversample mt pct:    {100*mt_final/max(len(post_er),1):.1f}%")
        print(f"  post-oversample er pct:    {100*er_final/max(len(post_er),1):.1f}%")
        print(f"\n=== DRY-RUN: re-run with --write to produce v4.5/. ===")
        return 0

    # Real write path: split, then oversample only train
    rng = random.Random(RANDOM_SEED)
    train, valid = _split_train_valid(capped, rng)

    rng_train = random.Random(RANDOM_SEED + 1)
    train, mt_added = _oversample_by_factor(
        train, lambda r: r.get("multi_turn"),
        MULTI_TURN_OVERSAMPLE_FACTOR, rng_train,
    )
    train, er_added = _oversample_by_factor(
        train, _is_error_recovery,
        ERROR_RECOVERY_OVERSAMPLE_FACTOR, rng_train,
    )
    train, project_natural, project_added = _oversample_project_tagged(train, rng_train)
    rng_train.shuffle(train)

    mt_train = sum(1 for r in train if r.get("multi_turn"))
    er_train = sum(1 for r in train if _is_error_recovery(r))

    train_final = _strip_audit_fields(train)
    valid_final = _strip_audit_fields(valid)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUTPUT_DIR / "train.chat.jsonl", train_final)
    _write_jsonl(OUTPUT_DIR / "valid.chat.jsonl", valid_final)

    # Tiny tier — filter against MAX_LENGTH=512 (trainer's tiny limit)
    from scripts.fine_tune.build_v3_dataset import _load_fix10_tokenizer
    _tok_tiny = _load_fix10_tokenizer()
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
        enc = _tok_tiny(text, truncation=True, max_length=512,
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

    train_ok = [r for r in train_final if _ok_at_512(r)]
    valid_ok = [r for r in valid_final if _ok_at_512(r)]
    rng_tiny = random.Random(RANDOM_SEED)
    tiny_train = rng_tiny.sample(train_ok, min(TINY_TRAIN_ROWS, len(train_ok)))
    tiny_valid = rng_tiny.sample(valid_ok, min(TINY_VALID_ROWS, len(valid_ok)))
    _write_jsonl(OUTPUT_DIR / "train.tiny.jsonl", tiny_train)
    _write_jsonl(OUTPUT_DIR / "valid.tiny.jsonl", tiny_valid)

    schemas_with_desc = {
        name: {**schema, "description": descriptions.get(name, f"Tool '{name}'.")}
        for name, schema in schemas.items()
    }
    with (OUTPUT_DIR / "tool_schemas.json").open("w") as f:
        json.dump(schemas_with_desc, f, indent=2)

    manifest = {
        "build_utc": datetime.now(timezone.utc).isoformat(),
        "base_target": "Qwen/Qwen3-4B (v4.5 multi-turn + live + error-recovery)",
        "v4_5_changes": [
            "Include retention_class='live' rows via LATERAL prompt linkage",
            f"Drop prompts shorter than {MIN_LIVE_PROMPT_LEN} chars",
            f"Oversample multi-turn rows {MULTI_TURN_OVERSAMPLE_FACTOR}x",
            f"Oversample error-recovery rows {ERROR_RECOVERY_OVERSAMPLE_FACTOR}x",
        ],
        "row_counts": {
            "fetched":              len(raw_rows),
            "by_retention_class":   dict(by_class),
            "converted":            len(converted),
            "after_short_filter":   len(short_session),
            "after_cap":            len(capped),
            "train_final":          len(train_final),
            "valid_final":          len(valid_final),
            "tiny_train":           len(tiny_train),
            "tiny_valid":           len(tiny_valid),
        },
        "multi_turn_stats": {
            "natural_in_converted":  mt_n,
            "oversample_added":      mt_added,
            "in_train_final":        mt_train,
            "pct_of_train":          round(mt_train / max(len(train_final), 1), 4),
            "gate":                  MIN_MULTI_TURN_PCT,
        },
        "error_recovery_stats": {
            "natural_in_converted":  er_n,
            "oversample_added":      er_added,
            "in_train_final":        er_train,
            "pct_of_train":          round(er_train / max(len(train_final), 1), 4),
        },
        "fix6_project_tagging": {
            "natural_per_project":   project_natural,
            "oversampled_added":     project_added,
        },
        "drops_per_fix": dict(drops),
        "tool_histogram_pre_oversample": hist,
        "output_sha256": {
            "train.chat.jsonl":  _stable_hash(train_final),
            "valid.chat.jsonl":  _stable_hash(valid_final),
            "train.tiny.jsonl":  _stable_hash(tiny_train),
            "valid.tiny.jsonl":  _stable_hash(tiny_valid),
        },
        "tuning_knobs": {
            "min_live_prompt_len":              MIN_LIVE_PROMPT_LEN,
            "multi_turn_oversample_factor":     MULTI_TURN_OVERSAMPLE_FACTOR,
            "error_recovery_oversample_factor": ERROR_RECOVERY_OVERSAMPLE_FACTOR,
            "min_multi_turn_pct_gate":          MIN_MULTI_TURN_PCT,
            "max_assistant_text_chars":         MAX_ASSISTANT_TEXT_CHARS,
            "per_tool_cap_fraction":            PER_TOOL_CAP_FRACTION,
        },
    }
    with (OUTPUT_DIR / "MANIFEST.json").open("w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n=== Wrote {OUTPUT_DIR} ===")
    for k, v in manifest["row_counts"].items():
        if isinstance(v, dict):
            print(f"  {k:24s} {v}")
        else:
            print(f"  {k:24s} {v:>6d}")
    print(f"\n  multi_turn_pct      {manifest['multi_turn_stats']['pct_of_train']:.2%}")
    print(f"  error_recovery_pct  {manifest['error_recovery_stats']['pct_of_train']:.2%}")
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
