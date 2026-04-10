#!/usr/bin/env python3
"""Prepare instruction-response JSONL for fine-tuning.

Supported inputs:
- agentMemory dataset JSONL (`sft`, `trajectory`, `preference`)
- Anvil session JSON (`.anvil/sessions/*.json`)
- Claude-like JSONL with role/content fields

Raw input files should live under data/raw/.
Outputs default to data/processed/fine_tune/.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.replace("\r", "\n").split())
    return json.dumps(value, ensure_ascii=False)


def _extract_from_agent_memory(rec: dict[str, Any]) -> tuple[str, str] | None:
    dtype = rec.get("dataset_type")
    if dtype == "sft":
        inp = rec.get("input", {})
        out = rec.get("output", {})
        prompt_text = _normalize_text(inp.get("prompt_text"))
        tool_name = _normalize_text(inp.get("tool_name"))
        instruction = prompt_text or f"Call tool `{tool_name}` with appropriate arguments."
        response = json.dumps(
            {
                "tool_name": tool_name,
                "tool_input": inp.get("tool_input"),
                "tool_response_preview": out.get("tool_response_preview"),
            },
            ensure_ascii=False,
        )
        return instruction, response

    if dtype == "trajectory":
        steps = rec.get("trajectory") or []
        if not steps:
            return None
        step = steps[0]
        instruction = _normalize_text(step.get("prompt_text")) or f"Execute `{step.get('tool_name', '')}`."
        response = json.dumps(
            {
                "tool_name": step.get("tool_name"),
                "tool_input": step.get("tool_input"),
                "tool_response_preview": step.get("tool_response_preview"),
                "outcome": rec.get("outcome"),
            },
            ensure_ascii=False,
        )
        return instruction, response

    if dtype == "preference":
        instruction = _normalize_text(rec.get("prompt_text")) or f"Prefer better behavior for tool `{rec.get('tool_name', '')}`."
        chosen = rec.get("chosen", {})
        response = json.dumps(
            {
                "tool_name": rec.get("tool_name"),
                "preferred_tool_input": chosen.get("tool_input"),
                "preferred_response": chosen.get("tool_response_preview"),
            },
            ensure_ascii=False,
        )
        return instruction, response

    return None


def _extract_role_content(obj: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        role = obj.get("role")
        content = obj.get("content")
        if isinstance(role, str) and isinstance(content, str):
            out.append((role.strip().lower(), content.strip()))
        for v in obj.values():
            out.extend(_extract_role_content(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_extract_role_content(item))
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


def _prepare_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    src = Path(args.input)
    records: list[dict[str, Any]] = []

    if args.input_format == "anvil_session":
        obj = json.loads(src.read_text(encoding="utf-8"))
        pairs = _pairs_from_role_content(_extract_role_content(obj.get("messages", [])))
        for instruction, response in pairs:
            records.append({"instruction": instruction, "response": response})
        return records

    if src.suffix == ".json" and args.input_format == "auto":
        obj = json.loads(src.read_text(encoding="utf-8"))
        pairs = _pairs_from_role_content(_extract_role_content(obj))
        for instruction, response in pairs:
            records.append({"instruction": instruction, "response": response})
        return records

    for rec in _read_jsonl(src):
        extracted: tuple[str, str] | None = None
        if args.input_format in {"auto", "agent_memory"}:
            extracted = _extract_from_agent_memory(rec)

        if extracted is None and args.input_format in {"auto", "claude_jsonl"}:
            pairs = _pairs_from_role_content(_extract_role_content(rec))
            if pairs:
                extracted = pairs[0]

        if extracted is None:
            continue

        instruction, response = extracted
        records.append({"instruction": instruction, "response": response})

    return records


def _dedupe_and_filter(records: list[dict[str, Any]], min_chars: int) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    cleaned: list[dict[str, Any]] = []
    for rec in records:
        instruction = _normalize_text(rec.get("instruction"))
        response = _normalize_text(rec.get("response"))
        if len(instruction) < min_chars or len(response) < min_chars:
            continue
        key = (instruction, response)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append({
            "instruction": instruction,
            "response": response,
            "messages": [
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": response},
            ],
        })
    return cleaned


def _write_jsonl(path: Path, records: list[dict[str, Any]], key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            if key == "instruction_response":
                payload = {
                    "instruction": rec["instruction"],
                    "response": rec["response"],
                }
            else:
                payload = rec[key]
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare fine-tune jsonl from raw data")
    parser.add_argument("--input", required=True, help="Input file under data/raw/")
    parser.add_argument(
        "--input-format",
        choices=["auto", "agent_memory", "anvil_session", "claude_jsonl"],
        default="auto",
    )
    parser.add_argument("--output-dir", default="data/processed/fine_tune")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-ratio", type=float, default=0.05)
    parser.add_argument("--min-chars", type=int, default=8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    random.seed(args.seed)

    records = _prepare_records(args)
    records = _dedupe_and_filter(records, min_chars=args.min_chars)
    random.shuffle(records)

    split_idx = int(len(records) * (1.0 - args.val_ratio))
    train_records = records[:split_idx]
    val_records = records[split_idx:]

    out_dir = Path(args.output_dir)
    _write_jsonl(out_dir / "train.instruction_response.jsonl", train_records, "instruction_response")
    _write_jsonl(out_dir / "valid.instruction_response.jsonl", val_records, "instruction_response")
    _write_jsonl(out_dir / "train.chat.jsonl", train_records, "messages")
    _write_jsonl(out_dir / "valid.chat.jsonl", val_records, "messages")

    stats = {
        "total_clean_records": len(records),
        "train_records": len(train_records),
        "val_records": len(val_records),
        "input": args.input,
        "input_format": args.input_format,
    }
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
