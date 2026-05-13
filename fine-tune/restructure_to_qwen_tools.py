#!/usr/bin/env python3
"""
restructure_to_qwen_tools.py

Transform `data/processed/fine_tune_blend/{train,valid}.chat.jsonl` (chat-format
rows where assistant messages contain JSON-stringified tool calls in `content`)
into Qwen 2.5 native tool-call format suitable for
`tokenizer.apply_chat_template(messages, tools=tools, tokenize=False)`.

Input row format (JSON array of message dicts per line):
    [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "{\"tool_name\": \"Bash\", \"tool_input\": {...}, \"tool_response_preview\": \"...\"}"}
    ]

Output row format (one JSON object per line):
    {
      "tools": [<JSON Schema for each tool used in this conversation>],
      "messages": [
        {"role": "system", "content": "You are Qwen, ..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "tool_calls": [
            {"type": "function", "function": {"name": "Bash", "arguments": {...}}}
        ]},
        {"role": "tool", "name": "Bash", "content": "<parsed preview>"}
      ]
    }

Outputs (written to data/processed/qwen25_tools/v1/):
  train.chat.jsonl, valid.chat.jsonl       (full)
  train.tiny.jsonl (200), valid.tiny.jsonl (30) — deterministic, seed=42
  tool_schemas.json   — JSON Schema Draft 7 per observed tool
  MANIFEST.json       — input/output SHA256, row counts, PII counts, git commit, UTC ts

Dependencies: stdlib + `jsonschema`.
Install (if missing):
    .venv-finetune/bin/pip install jsonschema

Run:
    .venv-finetune/bin/python fine-tune/restructure_to_qwen_tools.py
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

try:
    import jsonschema
    from jsonschema import Draft7Validator
except ImportError:
    print(
        "ERROR: jsonschema not installed. Run: .venv-finetune/bin/pip install jsonschema",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = REPO_ROOT / "data" / "processed" / "fine_tune_blend"
OUTPUT_DIR = REPO_ROOT / "data" / "processed" / "qwen25_tools" / "v1"

SYSTEM_PROMPT = (
    "You are Qwen, created by Alibaba Cloud. "
    "You are a helpful assistant with access to tools."
)

SEED = 42
TINY_TRAIN_N = 200
TINY_VALID_N = 30
REJECT_RATE_LIMIT = 0.05  # fail-fast if > 5%
SCHEMA_REQUIRED_PCT = 0.95  # field required if present in >= 95% of rows.
# 0.80 caused 5.35% rejection: Bash.description (82%) and Grep.output_mode (92%)
# straddled the boundary. 0.95 reflects "universally present by convention".


# ---------------------------------------------------------------------------
# PII scrubbing
# ---------------------------------------------------------------------------

PII_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("sk_token", re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "<REDACTED_TOKEN>"),
    ("bearer", re.compile(r"Bearer\s+\S+"), "<REDACTED_TOKEN>"),
    (
        "agent_memory_token",
        re.compile(r"AGENT_MEMORY_TOKEN[=:\s]*\S+"),
        "<REDACTED_TOKEN>",
    ),
    ("slack_xoxb", re.compile(r"xoxb-\S+"), "<REDACTED_TOKEN>"),
    ("github_pat", re.compile(r"ghp_[A-Za-z0-9]{36,}"), "<REDACTED_TOKEN>"),
    ("user_path", re.compile(r"/Users/mz/"), "/Users/<user>/"),
]


def scrub_string(s: str, counts: Counter) -> str:
    """Apply PII regex substitutions, incrementing per-pattern counts."""
    for name, pat, repl in PII_PATTERNS:
        new_s, n = pat.subn(repl, s)
        if n:
            counts[name] += n
            s = new_s
    return s


def scrub_value(value: Any, counts: Counter) -> Any:
    """Recursively scrub strings in any JSON-shaped value."""
    if isinstance(value, str):
        return scrub_string(value, counts)
    if isinstance(value, list):
        return [scrub_value(v, counts) for v in value]
    if isinstance(value, dict):
        return {k: scrub_value(v, counts) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_jsonl(path: Path) -> Iterable[Any]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as e:
                yield line_no, ("__parse_error__", str(e))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def get_git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------

def parse_assistant_tool_call(content: str) -> dict | None:
    """
    Try to parse a string-encoded tool call. Return dict with keys
    {tool_name, tool_input, tool_response_preview} or None if not a tool call.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if "tool_name" not in payload:
        return None
    return payload


def parse_tool_response(raw: Any) -> str:
    """
    Normalize a tool_response_preview to a string for the `tool` message content.

    - None -> ""
    - dict/list -> JSON-stringify
    - str that JSON-decodes to dict/list -> re-encode (canonical form)
    - other str -> verbatim
    """
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, sort_keys=True, ensure_ascii=False)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
        if isinstance(decoded, (dict, list)):
            return json.dumps(decoded, sort_keys=True, ensure_ascii=False)
        return raw
    return str(raw)


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------

PY_TO_JSON_TYPE = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "NoneType": "null",
}


def infer_tool_schemas(field_stats: dict[str, dict]) -> dict[str, dict]:
    """
    Build a JSON Schema Draft 7 per tool from observed field stats.

    field_stats[tool_name] = {
        "total": int,
        "fields": {field_name: {"count": int, "types": Counter}}
    }
    """
    schemas: dict[str, dict] = {}
    for tool_name, stats in sorted(field_stats.items()):
        total = stats["total"]
        properties: dict[str, dict] = {}
        required: list[str] = []
        for field_name, info in sorted(stats["fields"].items()):
            cnt = info["count"]
            types = info["types"]
            json_types = sorted({PY_TO_JSON_TYPE.get(t, "string") for t in types})
            if len(json_types) == 1:
                properties[field_name] = {"type": json_types[0]}
            else:
                properties[field_name] = {"type": json_types}
            if total and (cnt / total) >= SCHEMA_REQUIRED_PCT:
                required.append(field_name)
        schema: dict[str, Any] = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": tool_name,
            "type": "object",
            "properties": properties,
            "additionalProperties": True,
        }
        if required:
            schema["required"] = sorted(required)
        schemas[tool_name] = schema
    return schemas


def collect_field_stats(input_paths: list[Path]) -> dict[str, dict]:
    """Pass 1: scan inputs, count observed fields/types per tool."""
    stats: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "fields": defaultdict(lambda: {"count": 0, "types": Counter()})}
    )
    for path in input_paths:
        for _line_no, row in iter_jsonl(path):
            if isinstance(row, tuple) and row[0] == "__parse_error__":
                continue
            if not isinstance(row, list):
                continue
            for msg in row:
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                payload = parse_assistant_tool_call(msg.get("content", ""))
                if payload is None:
                    continue
                name = payload.get("tool_name") or ""
                if not name:
                    continue  # skip empty tool_name
                stats[name]["total"] += 1
                inp = payload.get("tool_input") or {}
                if isinstance(inp, dict):
                    for fname, fval in inp.items():
                        stats[name]["fields"][fname]["count"] += 1
                        stats[name]["fields"][fname]["types"][type(fval).__name__] += 1
    # Convert defaultdicts to plain dicts for deterministic output
    out: dict[str, dict] = {}
    for tn, s in stats.items():
        out[tn] = {
            "total": s["total"],
            "fields": {
                f: {"count": v["count"], "types": dict(v["types"])}
                for f, v in s["fields"].items()
            },
        }
    return out


# ---------------------------------------------------------------------------
# Row restructuring
# ---------------------------------------------------------------------------

def restructure_row(
    row: list[dict],
    validators: dict[str, Draft7Validator],
    tool_schemas: dict[str, dict],
    pii_counts: Counter,
) -> tuple[dict | None, str | None]:
    """
    Build the Qwen 2.5 chat dict from one input row.

    Returns (output_dict, None) on success, or (None, reason) on rejection.
    """
    if not isinstance(row, list) or not row:
        return None, "row_not_list"

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    used_tools: set[str] = set()

    has_user = False
    has_assistant = False

    for msg in row:
        if not isinstance(msg, dict):
            return None, "msg_not_dict"
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "user":
            messages.append({"role": "user", "content": scrub_value(content, pii_counts)})
            has_user = True
            continue

        if role == "system":
            # Preserve any existing system message after the prepended default.
            messages.append({"role": "system", "content": scrub_value(content, pii_counts)})
            continue

        if role == "assistant":
            has_assistant = True
            payload = parse_assistant_tool_call(content) if isinstance(content, str) else None
            if payload is None or not payload.get("tool_name"):
                # Plain-text assistant turn (not a tool call). Preserve content.
                if isinstance(content, str):
                    messages.append(
                        {"role": "assistant", "content": scrub_value(content, pii_counts)}
                    )
                else:
                    return None, "assistant_content_not_str"
                continue

            tool_name = payload["tool_name"]
            if tool_name not in tool_schemas:
                return None, f"unknown_tool:{tool_name}"

            raw_args = payload.get("tool_input") or {}
            if not isinstance(raw_args, dict):
                return None, "tool_input_not_dict"
            scrubbed_args = scrub_value(raw_args, pii_counts)

            # Validate against schema
            try:
                validators[tool_name].validate(scrubbed_args)
            except jsonschema.ValidationError as e:
                return None, f"schema_validation_failed:{tool_name}"

            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": scrubbed_args,
                            },
                        }
                    ],
                }
            )
            used_tools.add(tool_name)

            tool_response_text = parse_tool_response(payload.get("tool_response_preview"))
            scrubbed_response = scrub_value(tool_response_text, pii_counts)
            messages.append(
                {"role": "tool", "name": tool_name, "content": scrubbed_response}
            )
            continue

        if role == "tool":
            scrubbed = scrub_value(content, pii_counts)
            tool_msg = {"role": "tool", "content": scrubbed}
            if "name" in msg:
                tool_msg["name"] = msg["name"]
            messages.append(tool_msg)
            continue

        return None, f"unknown_role:{role}"

    if not has_user:
        return None, "missing_user_turn"
    if not has_assistant:
        return None, "missing_assistant_turn"

    tools_for_row = [tool_schemas[t] for t in sorted(used_tools)]
    out = {"tools": tools_for_row, "messages": messages}
    return out, None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def process_split(
    input_path: Path,
    output_path: Path,
    validators: dict[str, Draft7Validator],
    tool_schemas: dict[str, dict],
    pii_counts: Counter,
    rejection_counts: Counter,
) -> tuple[int, int, list[dict]]:
    """Return (input_count, kept_count, kept_rows). Does NOT write yet."""
    input_count = 0
    kept_rows: list[dict] = []
    for line_no, row in iter_jsonl(input_path):
        if isinstance(row, tuple) and row[0] == "__parse_error__":
            input_count += 1
            rejection_counts["json_parse_error"] += 1
            continue
        input_count += 1
        out, reason = restructure_row(row, validators, tool_schemas, pii_counts)
        if out is None:
            rejection_counts[reason or "unknown"] += 1
            continue
        kept_rows.append(out)
    return input_count, len(kept_rows), kept_rows


def deterministic_sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Stable, reproducible sample without mutating input order."""
    if n >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    return rng.sample(rows, n)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--tiny-train", type=int, default=TINY_TRAIN_N)
    parser.add_argument("--tiny-valid", type=int, default=TINY_VALID_N)
    parser.add_argument(
        "--reject-rate-limit",
        type=float,
        default=REJECT_RATE_LIMIT,
        help="Abort if rejected/input exceeds this ratio (default 0.05).",
    )
    args = parser.parse_args(argv)

    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    train_in = input_dir / "train.chat.jsonl"
    valid_in = input_dir / "valid.chat.jsonl"
    for p in (train_in, valid_in):
        if not p.exists():
            print(f"ERROR: missing input file: {p}", file=sys.stderr)
            return 2

    print(f"[1/4] Scanning inputs for schema inference: {train_in}, {valid_in}")
    field_stats = collect_field_stats([train_in, valid_in])
    tool_schemas = infer_tool_schemas(field_stats)
    print(f"      Observed {len(tool_schemas)} tools: {sorted(tool_schemas)}")

    validators = {name: Draft7Validator(schema) for name, schema in tool_schemas.items()}

    pii_counts: Counter = Counter()
    rejection_counts: Counter = Counter()

    print(f"[2/4] Restructuring rows...")
    train_in_n, train_kept_n, train_rows = process_split(
        train_in, output_dir / "train.chat.jsonl",
        validators, tool_schemas, pii_counts, rejection_counts,
    )
    valid_in_n, valid_kept_n, valid_rows = process_split(
        valid_in, output_dir / "valid.chat.jsonl",
        validators, tool_schemas, pii_counts, rejection_counts,
    )

    total_in = train_in_n + valid_in_n
    total_kept = train_kept_n + valid_kept_n
    total_rejected = total_in - total_kept
    reject_rate = total_rejected / total_in if total_in else 0.0
    print(
        f"      train: in={train_in_n} kept={train_kept_n} rejected={train_in_n - train_kept_n}"
    )
    print(
        f"      valid: in={valid_in_n} kept={valid_kept_n} rejected={valid_in_n - valid_kept_n}"
    )
    print(f"      total: in={total_in} kept={total_kept} rejected={total_rejected} rate={reject_rate:.4%}")
    print(f"      rejection reasons: {dict(rejection_counts)}")

    if reject_rate > args.reject_rate_limit:
        print(
            f"FAIL: rejection rate {reject_rate:.2%} exceeds limit {args.reject_rate_limit:.2%}; "
            f"refusing to write outputs.",
            file=sys.stderr,
        )
        return 3

    print(f"[3/4] Writing outputs to {output_dir}")
    train_out = output_dir / "train.chat.jsonl"
    valid_out = output_dir / "valid.chat.jsonl"
    train_tiny_out = output_dir / "train.tiny.jsonl"
    valid_tiny_out = output_dir / "valid.tiny.jsonl"
    tool_schemas_out = output_dir / "tool_schemas.json"

    write_jsonl(train_out, train_rows)
    write_jsonl(valid_out, valid_rows)
    write_jsonl(train_tiny_out, deterministic_sample(train_rows, args.tiny_train, args.seed))
    write_jsonl(valid_tiny_out, deterministic_sample(valid_rows, args.tiny_valid, args.seed))

    with tool_schemas_out.open("w", encoding="utf-8") as f:
        json.dump(tool_schemas, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"[4/4] Writing manifest")
    manifest = {
        "script": "fine-tune/restructure_to_qwen_tools.py",
        "git_commit": get_git_commit(),
        "utc_timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "seed": args.seed,
        "input_sha256": {
            "train.chat.jsonl": sha256_file(train_in),
            "valid.chat.jsonl": sha256_file(valid_in),
        },
        "output_sha256": {
            "train.chat.jsonl": sha256_file(train_out),
            "valid.chat.jsonl": sha256_file(valid_out),
            "train.tiny.jsonl": sha256_file(train_tiny_out),
            "valid.tiny.jsonl": sha256_file(valid_tiny_out),
            "tool_schemas.json": sha256_file(tool_schemas_out),
        },
        "row_counts": {
            "input": {
                "train": train_in_n,
                "valid": valid_in_n,
                "total": total_in,
            },
            "output": {
                "train": train_kept_n,
                "valid": valid_kept_n,
                "total": total_kept,
            },
            "rejected": {
                "train": train_in_n - train_kept_n,
                "valid": valid_in_n - valid_kept_n,
                "total": total_rejected,
            },
        },
        "rejected_row_reasons": dict(rejection_counts),
        "rejection_rate": reject_rate,
        "rejection_rate_limit": args.reject_rate_limit,
        "pii_substitutions": dict(pii_counts),
        "tool_schemas_summary": {
            name: {
                "required": sorted(schema.get("required", [])),
                "all_fields": sorted((schema.get("properties") or {}).keys()),
                "observed_rows": field_stats[name]["total"],
            }
            for name, schema in tool_schemas.items()
        },
    }
    manifest_out = output_dir / "MANIFEST.json"
    with manifest_out.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, sort_keys=True, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"OK: wrote outputs to {output_dir}")
    print(f"OK: manifest at {manifest_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
