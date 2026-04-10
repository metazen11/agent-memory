#!/usr/bin/env python3
"""Diagnose common local fine-tuning failures from log files."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

RULES: list[tuple[str, str, str]] = [
    (
        r"(out of memory|oom|cuda out of memory|metal.*allocation failed)",
        "memory",
        "Reduce --max-seq-length, --batch-size, and --lora-rank. On 16GB Macs start with batch=1, seq=1024, rank=8.",
    ),
    (
        r"(No module named|ModuleNotFoundError)",
        "dependency",
        "Install missing dependencies in the active venv (e.g. `pip install mlx mlx-tune`).",
    ),
    (
        r"(401|403|unauthorized|forbidden|gated repo)",
        "auth",
        "Set Hugging Face token (`export HUGGING_FACE_API=...` or `export HF_TOKEN=...`) and accept model license.",
    ),
    (
        r"(No such file or directory|not found)",
        "path",
        "Verify model path, dataset path, and output directory. Keep raw data under data/raw and processed under data/processed.",
    ),
    (
        r"(SSL|CERTIFICATE_VERIFY_FAILED|ConnectionError|timed out)",
        "network",
        "Check network/VPN/proxy. Retry model download with stable connection.",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug fine-tuning logs")
    parser.add_argument("--log", required=True, help="Path to log file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    text = Path(args.log).read_text(encoding="utf-8", errors="ignore")

    findings: list[dict[str, str]] = []
    for pattern, code, suggestion in RULES:
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            findings.append(
                {
                    "issue": code,
                    "pattern": pattern,
                    "suggestion": suggestion,
                }
            )

    if not findings:
        findings.append(
            {
                "issue": "unknown",
                "pattern": "n/a",
                "suggestion": "No known pattern matched. Share the last 200 log lines for manual diagnosis.",
            }
        )

    print(json.dumps({"log": args.log, "findings": findings}, indent=2))


if __name__ == "__main__":
    main()
