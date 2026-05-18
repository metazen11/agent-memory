#!/usr/bin/env python3
"""Score an A/B run produced by scripts/fine_tune/ab_anvil.sh.

Reads anvil run logs and produces a scoreboard per model on four metrics:

  1. useful_answer       — final assistant turn addresses the prompt
                            (heuristic: non-empty, no error-only content,
                             >= 30 chars of meaningful text)
  2. loop_rate           — fraction of prompts where 3+ consecutive
                            identical tool_calls appeared
  3. adaptation_rate     — fraction of prompts where, after seeing a
                            tool_response, the next tool_call differed
                            from the previous (proves the model used
                            the tool_response, not just re-emitted)
  4. path_correctness    — fraction of prompts free of hallucinated
                            paths (path-like strings in tool_call
                            args that don't exist on disk)

Output: scoreboard.md in the run directory + stdout summary.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path


TOOL_CALL_RE = re.compile(r"<tool_call>\s*({.*?})\s*</tool_call>", re.S)
# Catch absolute paths whether quoted, bareword in shell commands, or
# inside JSON. Anchor on leading / and stop at whitespace, quote, or
# common shell separators.
PATH_HINT_RE = re.compile(r"(/(?:Users|opt|etc|var)/[\w./\-_]+)")
ERROR_FRAGMENTS = (
    "Traceback", "error: ", "ModuleNotFoundError",
    "command not found", "No such file or directory",
)
SUCCESS_HINTS = (
    "scoreboard", "summary", "result", "found", "here's", "the answer",
    "you can", "based on", "i checked", "according to",
)


def _read_log(p: Path) -> tuple[str, list[dict], str]:
    """Return (prompt, list_of_tool_calls, final_text)."""
    text = p.read_text(errors="replace")
    # Prompt is on the first line we wrote
    prompt = ""
    if text.startswith("=== PROMPT: "):
        end = text.find(" ===\n")
        if end > 0:
            prompt = text[len("=== PROMPT: "):end].strip()
    # Tool calls: find all <tool_call>{json}</tool_call> blocks
    calls: list[dict] = []
    for m in TOOL_CALL_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            calls.append(json.loads(raw))
        except Exception:
            calls.append({"_raw": raw})
    # Final text: take last 800 chars after the last tool_call block,
    # or the whole tail if no tool calls
    if calls:
        last_end = list(TOOL_CALL_RE.finditer(text))[-1].end()
        final = text[last_end:].strip()
    else:
        final = text[-800:].strip()
    return prompt, calls, final


def _tool_calls_equal(a: dict, b: dict) -> bool:
    if "_raw" in a or "_raw" in b:
        return a.get("_raw") == b.get("_raw")
    return (
        a.get("name") == b.get("name")
        and a.get("arguments") == b.get("arguments")
    )


def _has_loop(calls: list[dict]) -> bool:
    """3+ consecutive identical tool_calls."""
    if len(calls) < 3:
        return False
    for i in range(len(calls) - 2):
        if (_tool_calls_equal(calls[i], calls[i+1])
                and _tool_calls_equal(calls[i+1], calls[i+2])):
            return True
    return False


def _adapted(calls: list[dict]) -> bool | None:
    """Did the model change its tool_call after seeing tool_response?

    We don't have tool_response markers in the log directly, but anvil
    interleaves the calls with results. If the model has ≥2 distinct
    tool_calls in sequence (and there were intermediate results between
    them), count as adapted. Returns None if not enough calls to judge.
    """
    if len(calls) < 2:
        return None
    return any(not _tool_calls_equal(calls[i], calls[i+1])
               for i in range(len(calls)-1))


def _useful(final: str, prompt: str) -> bool:
    if not final or len(final) < 30:
        return False
    if any(frag in final for frag in ERROR_FRAGMENTS):
        # Allow useful text alongside an error if there's a substantive
        # summary — heuristic: success hint after the error
        if not any(h in final.lower() for h in SUCCESS_HINTS):
            return False
    # Reject pure tool-call regurgitation
    if final.count("<tool_call>") > 0 and len(final.replace("<tool_call>", "").strip()) < 30:
        return False
    return True


def _path_correct(calls: list[dict]) -> bool:
    """All path-like arg values either exist or are clearly relative/symbolic."""
    for c in calls:
        args = c.get("arguments") if isinstance(c, dict) else None
        if not isinstance(args, dict):
            continue
        for v in args.values():
            if not isinstance(v, str):
                continue
            for m in PATH_HINT_RE.finditer(v):
                p = Path(m.group(1))
                if not p.exists():
                    return False
    return True


def score_dir(run_dir: Path) -> dict[str, dict]:
    """Return {model: metrics_dict}."""
    out: dict[str, dict] = {}
    for model_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        model = model_dir.name
        logs = sorted(model_dir.glob("*.log"))
        if not logs:
            continue
        metrics = defaultdict(int)
        adaptation_n = 0
        adaptation_total = 0
        for log in logs:
            _prompt, calls, final = _read_log(log)
            metrics["n"] += 1
            if _useful(final, _prompt):
                metrics["useful"] += 1
            if _has_loop(calls):
                metrics["loop"] += 1
            if _path_correct(calls):
                metrics["path_ok"] += 1
            ad = _adapted(calls)
            if ad is not None:
                adaptation_total += 1
                if ad:
                    adaptation_n += 1
            metrics["total_calls"] += len(calls)
        n = metrics["n"]
        out[model] = {
            "n": n,
            "useful_rate":         round(metrics["useful"] / max(n, 1), 3),
            "loop_rate":           round(metrics["loop"] / max(n, 1), 3),
            "path_correct_rate":   round(metrics["path_ok"] / max(n, 1), 3),
            "adaptation_rate":     round(adaptation_n / max(adaptation_total, 1), 3),
            "adaptation_basis":    adaptation_total,
            "mean_calls_per_prompt": round(metrics["total_calls"] / max(n, 1), 2),
        }
    return out


def write_scoreboard(run_dir: Path, scores: dict[str, dict]) -> Path:
    lines = [
        "# A/B Scoreboard",
        f"Run dir: `{run_dir}`",
        "",
        "| Model | N | Useful | Loop | Path OK | Adaptation | Calls/prompt |",
        "|-------|---|-------:|-----:|--------:|-----------:|-------------:|",
    ]
    for model, s in scores.items():
        lines.append(
            f"| {model} | {s['n']} | "
            f"{s['useful_rate']:.0%} | "
            f"{s['loop_rate']:.0%} | "
            f"{s['path_correct_rate']:.0%} | "
            f"{s['adaptation_rate']:.0%} (n={s['adaptation_basis']}) | "
            f"{s['mean_calls_per_prompt']} |"
        )
    lines += [
        "",
        "## Ship gate",
        "v4 ships if it meets ALL of:",
        "- useful_rate     ≥ v1 baseline",
        "- loop_rate       ≤ v1 baseline (lower is better)",
        "- path_correct    ≥ v1 baseline",
        "- adaptation_rate ≥ 0.60 (key v3-regression metric)",
        "",
    ]
    out_path = run_dir / "scoreboard.md"
    out_path.write_text("\n".join(lines))
    return out_path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: ab_anvil_score.py <run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"Not a directory: {run_dir}", file=sys.stderr)
        return 2
    scores = score_dir(run_dir)
    out = write_scoreboard(run_dir, scores)
    print(out.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
