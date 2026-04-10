#!/usr/bin/env python3
"""Build dataset from all Claude JSONL logs in a directory.

Input: data/raw/claude/*.jsonl
Output: data/processed/claude_all/{train,valid}.{instruction_response,chat}.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.replace("\r", "\n").split())
    return json.dumps(value, ensure_ascii=False)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_blocks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    text_blocks.append(text.strip())
        return "\n".join(text_blocks).strip()
    return ""


def _event_to_messages(obj: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    # Direct role/content event
    role = obj.get("role")
    if isinstance(role, str):
        text = _content_to_text(obj.get("content"))
        if text:
            out.append((role.strip().lower(), text))

    # Claude JSONL commonly stores turn under `message`
    msg = obj.get("message")
    if isinstance(msg, dict):
        msg_role = msg.get("role")
        if isinstance(msg_role, str):
            text = _content_to_text(msg.get("content"))
            if text:
                out.append((msg_role.strip().lower(), text))

    return out


def _pairs_from_role_content(messages: list[tuple[str, str]]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for role, content in messages:
        if not content:
            continue
        if role == "user":
            pending_user = content
        elif role == "assistant" and pending_user:
            pairs.append((pending_user, content))
            pending_user = None
    return pairs


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _write_jsonl(path: Path, rows: list[dict], key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            if key == "instruction_response":
                payload = {"instruction": r["instruction"], "response": r["response"]}
            else:
                payload = r[key]
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Prepare dataset from all Claude JSONL logs")
    p.add_argument("--input-dir", default="data/raw/claude")
    p.add_argument("--output-dir", default="data/processed/claude_all")
    p.add_argument("--min-chars", type=int, default=8)
    p.add_argument("--val-ratio", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-files", type=int, default=0, help="0 means all files")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    input_dir = Path(args.input_dir)
    files = sorted(input_dir.glob("*.jsonl"))
    if args.max_files > 0:
        files = files[: args.max_files]

    records: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for fp in files:
        ordered_msgs: list[tuple[str, str]] = []
        for obj in _iter_jsonl(fp):
            if isinstance(obj, dict):
                ordered_msgs.extend(_event_to_messages(obj))

        pairs = _pairs_from_role_content(ordered_msgs)
        for user, assistant in pairs:
                instruction = _normalize_text(user)
                response = _normalize_text(assistant)
                if len(instruction) < args.min_chars or len(response) < args.min_chars:
                    continue
                key = (instruction, response)
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    {
                        "instruction": instruction,
                        "response": response,
                        "messages": [
                            {"role": "user", "content": instruction},
                            {"role": "assistant", "content": response},
                        ],
                    }
                )

    random.shuffle(records)
    split = int(len(records) * (1.0 - args.val_ratio))
    train, valid = records[:split], records[split:]

    out_dir = Path(args.output_dir)
    _write_jsonl(out_dir / "train.instruction_response.jsonl", train, "instruction_response")
    _write_jsonl(out_dir / "valid.instruction_response.jsonl", valid, "instruction_response")
    _write_jsonl(out_dir / "train.chat.jsonl", train, "messages")
    _write_jsonl(out_dir / "valid.chat.jsonl", valid, "messages")

    stats = {
        "input_dir": str(input_dir),
        "files_used": len(files),
        "total_clean_records": len(records),
        "train_records": len(train),
        "val_records": len(valid),
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
