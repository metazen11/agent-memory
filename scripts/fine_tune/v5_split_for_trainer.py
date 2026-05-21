#!/usr/bin/env python3
"""Place v5 pilot dataset into the trainer's expected layout.

The trainer (run_train_lora.py) expects:
    data/processed/qwen25_tools/<VERSION>/train.chat.jsonl
    data/processed/qwen25_tools/<VERSION>/valid.chat.jsonl
    data/processed/qwen25_tools/<VERSION>/train.tiny.jsonl
    data/processed/qwen25_tools/<VERSION>/valid.tiny.jsonl

This script:
  - 90/10 random split (seeded) of datasets/v5_pilot/train.jsonl
  - writes the full split as train.chat.jsonl + valid.chat.jsonl
  - writes a smoke-sized subset as train.tiny.jsonl (50 rows) + valid.tiny.jsonl (10)

Usage:
    python3 scripts/fine_tune/v5_split_for_trainer.py
    python3 scripts/fine_tune/v5_split_for_trainer.py --version v5-pilot
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default="datasets/v5_pilot/train.jsonl",
                    help="Source jsonl (default: datasets/v5_pilot/train.jsonl)")
    ap.add_argument("--family", default="qwen25_tools",
                    help="Trainer's expected dataset family (default: qwen25_tools)")
    ap.add_argument("--version", default="v5-pilot",
                    help="Dataset version subdir (default: v5-pilot)")
    ap.add_argument("--valid-frac", type=float, default=0.1,
                    help="Fraction held out for validation (default: 0.1)")
    ap.add_argument("--tiny-train", type=int, default=50,
                    help="Rows in tiny train set (default: 50)")
    ap.add_argument("--tiny-valid", type=int, default=10,
                    help="Rows in tiny valid set (default: 10)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    src = REPO_ROOT / args.src
    if not src.exists():
        print(f"FAIL: {src} not found", file=sys.stderr)
        return 1

    rows = src.read_text().splitlines()
    rows = [r for r in rows if r.strip()]
    print(f"[load] {len(rows)} rows from {src}")

    rng = random.Random(args.seed)
    rng.shuffle(rows)
    n_valid = max(1, int(len(rows) * args.valid_frac))
    valid_rows = rows[:n_valid]
    train_rows = rows[n_valid:]
    print(f"[split] train={len(train_rows)} valid={len(valid_rows)}")

    dst_dir = REPO_ROOT / "data" / "processed" / args.family / args.version
    dst_dir.mkdir(parents=True, exist_ok=True)

    full_train = dst_dir / "train.chat.jsonl"
    full_valid = dst_dir / "valid.chat.jsonl"
    tiny_train = dst_dir / "train.tiny.jsonl"
    tiny_valid = dst_dir / "valid.tiny.jsonl"

    full_train.write_text("\n".join(train_rows) + "\n")
    full_valid.write_text("\n".join(valid_rows) + "\n")
    tiny_train.write_text("\n".join(train_rows[:args.tiny_train]) + "\n")
    tiny_valid.write_text("\n".join(valid_rows[:args.tiny_valid]) + "\n")

    print(f"[write] {full_train} ({len(train_rows)} rows)")
    print(f"[write] {full_valid} ({len(valid_rows)} rows)")
    print(f"[write] {tiny_train} ({min(args.tiny_train, len(train_rows))} rows)")
    print(f"[write] {tiny_valid} ({min(args.tiny_valid, len(valid_rows))} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
