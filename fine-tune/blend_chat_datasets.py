#!/usr/bin/env python3
"""Blend multiple chat JSONL datasets with optional repetition weights."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def _read_chat_jsonl(path: Path) -> list[list[dict]]:
    rows: list[list[dict]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, list):
                rows.append(obj)
    return rows


def _normalize_messages(msgs: list[dict]) -> tuple[str, str] | None:
    user = ""
    assistant = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", "")).strip().lower()
        content = str(m.get("content", "")).strip()
        if role == "user" and content:
            user = content
        elif role == "assistant" and content:
            assistant = content
            if user:
                return user, assistant
    return None


def _write(path: Path, rows: list[dict], key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            if key == "chat":
                payload = r["messages"]
            else:
                payload = {"instruction": r["instruction"], "response": r["response"]}
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Blend chat datasets")
    p.add_argument("--source", action="append", required=True, help="path[:weight], e.g. data/a/train.chat.jsonl:2")
    p.add_argument("--output-dir", default="data/processed/fine_tune_blend")
    p.add_argument("--val-ratio", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    rows: list[dict] = []
    source_stats = {}

    for src in args.source:
        if ":" in src:
            path_str, w_str = src.rsplit(":", 1)
            weight = max(1, int(w_str))
        else:
            path_str, weight = src, 1

        path = Path(path_str)
        raw = _read_chat_jsonl(path)
        source_stats[str(path)] = {"raw_rows": len(raw), "weight": weight}

        norm_rows = []
        source_seen: set[tuple[str, str]] = set()
        for msgs in raw:
            pair = _normalize_messages(msgs)
            if not pair:
                continue
            user, assistant = pair
            key = (" ".join(user.split()), " ".join(assistant.split()))
            if key in source_seen:
                continue
            source_seen.add(key)
            norm_rows.append(
                {
                    "instruction": key[0],
                    "response": key[1],
                    "messages": [
                        {"role": "user", "content": key[0]},
                        {"role": "assistant", "content": key[1]},
                    ],
                }
            )

        # weighted oversampling after dedupe within source
        for _ in range(weight):
            rows.extend(norm_rows)

        source_stats[str(path)]["unique_rows"] = len(norm_rows)
        source_stats[str(path)]["expanded_rows"] = len(norm_rows) * weight

    random.shuffle(rows)
    split = int(len(rows) * (1.0 - args.val_ratio))
    train, valid = rows[:split], rows[split:]

    out = Path(args.output_dir)
    _write(out / "train.chat.jsonl", train, "chat")
    _write(out / "valid.chat.jsonl", valid, "chat")
    _write(out / "train.instruction_response.jsonl", train, "ir")
    _write(out / "valid.instruction_response.jsonl", valid, "ir")

    stats = {
        "sources": source_stats,
        "total_rows": len(rows),
        "train_rows": len(train),
        "valid_rows": len(valid),
        "val_ratio": args.val_ratio,
    }
    (out / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
