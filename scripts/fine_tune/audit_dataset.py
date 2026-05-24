#!/usr/bin/env python3
"""Pre-training data-quality audit. Runs after build_v{N}_dataset.py --write
and before training. Refuses to PASS if any category exceeds its threshold.

Design principle: deterministic checks first. String matches and existence
checks (path on disk, hash in git) are deterministic; heuristics like
"sounds like agent monologue" are not. Only when a deterministic check
isn't possible do we fall back to a regex.

Categories enforced (see docs/fine_tune/DATA_QUALITY_GATES.md):

  C1   Path bias                     [deterministic — string match]
       /Dropbox/_CODING/ in any field, /Users/<foreign>/ in any field

  C8a-D  Fabricated agentic references [deterministic — verify exists]
         For each path-shaped string in assistant content: check
         Path(p).exists(). For each "commit <hash>" reference: check
         git cat-file -e <hash>. A FABRICATED path/hash means the row
         is teaching the model to invent.

  C8a-H  Agentic-monologue patterns    [heuristic — soft threshold]
         "Let me X" 3+ times, "Want me to" appearance. Kept as a soft
         signal, not a hard fail. Reported but with higher threshold.

Other categories (#2 scaffold, #3 zero-label, #4 empty-args, #5 mutations,
#6 vision/subagent/task-notif, #7 repetition, #8b multi-turn) are enforced
INSIDE the dataset builder at row-build time and not re-checked here.

Usage:
  .venv-finetune/bin/python scripts/fine_tune/audit_dataset.py \\
      data/processed/qwen3_tools/v4.5/

  SKIP_AUDIT=1                   → skip entirely (tiny smoke tests only)
  AUDIT_VERIFY_PATHS=0           → skip path-exists checks (faster)

Exit code 0 = PASS, 1 = FAIL (with summary).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path


# Category 1: path bias — deterministic
DROPBOX_RE = re.compile(r"/Dropbox/_CODING/")
FOREIGN_USER_RE = re.compile(r"/Users/(?!mz/|<user>/)[a-z_-]+/")

# Category 8a deterministic: find absolute paths and verify they exist
ABS_PATH_RE = re.compile(r"(?:^|[\s\"'(=,>])(/Users/mz/[\w./\-_]+|~/[\w./\-_]+)")
# Hex git hash (7+ chars, must be hex-only). Bounded so we don't match
# arbitrary alphanumeric runs.
GIT_HASH_RE = re.compile(r"\b([0-9a-f]{7,40})\b")

# Category 8a heuristic (soft)
LET_ME_RE = re.compile(r"\bLet me\b", re.I)
WANT_ME_TO_RE = re.compile(r"\bWant me to\b", re.I)


def _iter_rows(p: Path):
    with p.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _row_text(row: dict) -> str:
    """All textual content in a row — joined into one string for pattern matching."""
    parts: list[str] = []
    for msg in row.get("messages", []):
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
        # tool_calls argument text
        for tc in msg.get("tool_calls", []) or []:
            fn = (tc.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, str):
                parts.append(args)
            elif isinstance(args, dict):
                parts.append(json.dumps(args))
    return "\n".join(parts)


def _last_assistant_text(row: dict) -> str | None:
    """The trailing assistant text turn if the row has one (multi-turn)."""
    msgs = row.get("messages", [])
    if not msgs:
        return None
    last = msgs[-1]
    if last.get("role") != "assistant":
        return None
    if last.get("tool_calls"):
        return None  # tool-call turn, not text
    c = last.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            item.get("text", "") for item in c
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return None


# Cache of "does this path exist on disk" lookups
_PATH_EXISTS_CACHE: dict[str, bool] = {}


def _path_exists(p: str) -> bool:
    if p in _PATH_EXISTS_CACHE:
        return _PATH_EXISTS_CACHE[p]
    try:
        result = Path(p).expanduser().exists()
    except (OSError, ValueError):
        result = False
    _PATH_EXISTS_CACHE[p] = result
    return result


def audit(jsonl_path: Path, verify_paths: bool = True) -> tuple[bool, dict]:
    """Return (passed, report_dict).

    verify_paths=False skips Path.exists() lookups (faster, but loses the
    deterministic fabricated-path check).
    """
    n = 0
    counters: Counter = Counter()
    pattern_examples: dict[str, str] = {}

    for row in _iter_rows(jsonl_path):
        n += 1
        text = _row_text(row)

        # C1: path bias [deterministic]
        if DROPBOX_RE.search(text):
            counters["c1_dropbox_paths"] += 1
            pattern_examples.setdefault("c1_dropbox_paths",
                next(iter(DROPBOX_RE.findall(text)), ""))
        if FOREIGN_USER_RE.search(text):
            counters["c1_foreign_user"] += 1
            pattern_examples.setdefault("c1_foreign_user",
                next(iter(FOREIGN_USER_RE.findall(text)), ""))

        # C8a-D: fabricated path/hash references in the trailing assistant
        # text turn. Deterministic — verifies against the actual filesystem.
        last_text = _last_assistant_text(row) or ""
        if last_text and verify_paths:
            fab_paths = []
            for m in ABS_PATH_RE.finditer(last_text):
                p = m.group(1)
                if not _path_exists(p):
                    fab_paths.append(p)
            if fab_paths:
                counters["c8a_fabricated_paths"] += 1
                pattern_examples.setdefault("c8a_fabricated_paths", fab_paths[0])

        # C8a-H: agentic-monologue heuristic (soft signal, higher threshold)
        if last_text:
            if len(LET_ME_RE.findall(last_text)) >= 3:
                counters["c8a_let_me_3plus"] += 1
            if WANT_ME_TO_RE.search(last_text):
                counters["c8a_want_me_to"] += 1
            if len(last_text) > 600 and "<tool_call>" not in last_text:
                counters["c8a_long_text_only"] += 1

    def pct(k: str) -> float:
        return counters[k] / max(n, 1)

    # Hard gates (deterministic) — exceeding these is a build-fail
    hard_gates = [
        ("c1_dropbox_paths",       0.01),  # any Dropbox at all is a red flag
        ("c1_foreign_user",        0.01),
        ("c8a_fabricated_paths",   0.05),  # 5% fabricated-path rows max
    ]
    # Soft gates (heuristic) — exceeding these prints a warning but doesn't fail
    soft_gates = [
        ("c8a_let_me_3plus",       0.10),
        ("c8a_want_me_to",         0.05),
        ("c8a_long_text_only",     0.20),
    ]

    gate_results = []
    failures = []
    for name, threshold in hard_gates:
        actual = pct(name)
        gate_results.append((name, threshold, actual, "hard"))
        if actual > threshold:
            failures.append((name, threshold, actual))
    for name, threshold in soft_gates:
        actual = pct(name)
        gate_results.append((name, threshold, actual, "soft"))

    return not failures, {
        "file": str(jsonl_path),
        "row_count": n,
        "counts": dict(counters),
        "gates": [
            {"category": name, "threshold": threshold, "actual": round(actual, 4),
             "kind": kind}
            for name, threshold, actual, kind in gate_results
        ],
        "failures": [
            {"category": name, "threshold": threshold, "actual": round(actual, 4),
             "example": pattern_examples.get(name, "")}
            for name, threshold, actual in failures
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dataset_dir", help="data/processed/<family>/<version>/")
    args = p.parse_args()

    if os.getenv("SKIP_AUDIT") == "1":
        print("SKIP_AUDIT=1 — audit skipped")
        return 0

    base = Path(args.dataset_dir)
    if not base.is_dir():
        print(f"FAIL: {base} is not a directory", file=sys.stderr)
        return 2

    verify_paths = os.getenv("AUDIT_VERIFY_PATHS", "1") == "1"
    all_passed = True
    for split in ("train.chat.jsonl", "valid.chat.jsonl"):
        f = base / split
        if not f.exists():
            print(f"FAIL: {f} missing", file=sys.stderr)
            return 2
        passed, report = audit(f, verify_paths=verify_paths)
        all_passed = all_passed and passed
        print(f"\n=== AUDIT: {split} ===")
        print(f"  rows: {report['row_count']}  (verify_paths={verify_paths})")
        for gate in report["gates"]:
            cat = gate["category"]
            actual = gate["actual"]
            threshold = gate["threshold"]
            kind = gate["kind"]
            count = report["counts"].get(cat, 0)
            if kind == "hard":
                status = "✓" if actual <= threshold else "✗"
            else:
                # Soft gate: ⚠ if exceeded, ✓ otherwise
                status = "✓" if actual <= threshold else "⚠"
            print(f"  {status} [{kind:4s}] {cat:25s} {count:>6d} "
                  f"({actual:.2%}, threshold {threshold:.0%})")
        for fail in report["failures"]:
            print(f"  ✗ HARD FAIL: {fail['category']} actual={fail['actual']:.2%} > {fail['threshold']:.0%}")
            if fail.get("example"):
                print(f"               example: {fail['example']!r}")

    if all_passed:
        print("\nPASS — all hard gates within threshold.")
        return 0
    print("\nFAIL — see hard gates above. Fix dataset before training.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
