#!/usr/bin/env python3
"""Harder eval harness — discriminates v4-class models that saturate ab_multiturn.

The 10-scenario multi-turn harness (ab_multiturn.py) scores all v4 checkpoints
at 10/10, so it can't distinguish v4 from v4.5 candidates. This harness uses
scenario fixtures with *deterministic* failure signatures targeting specific
v4 regressions we've observed.

Scenario categories (one fixture file per category):
  path_bias_scenarios.jsonl    — model rewrites user-typed paths (the v4 bug)
  cross_project_scenarios.jsonl     — name exists in N projects, model picks wrong one
  ood_project_scenarios.jsonl       — project not in training, model should not fabricate
  fabrication_scenarios.jsonl       — vague prompt, model invents PRs/branches/files

Each fixture row:
  id                       (str)        — stable identifier
  user                     (str)        — the prompt
  expected_path_in_args    (str|null)   — if non-null, this exact substring must
                                          appear in *some* tool_call's args
  forbidden_substrings     (list[str])  — none of these may appear anywhere in
                                          response text OR tool_call args
  rationale                (str)        — operator-facing explanation

Scoring per scenario (deterministic):
  PASS if (no forbidden substring fires) AND (expected_path_in_args is None
          OR appears in some tool_call's args.values() concatenated)
  FAIL otherwise.

Tied to llama-server :9099 (same port as ab_multiturn.py — DO NOT run both
concurrently). One model at a time.

Usage:
  python scripts/fine_tune/eval_harder.py --models v4
  python scripts/fine_tune/eval_harder.py --models v4 --categories path_bias
  python scripts/fine_tune/eval_harder.py --model v4=models/gguf/qwen3-4b-toolcalls-v4-q6k.gguf \\
                                          --model v4.5=models/gguf/qwen3-4b-toolcalls-v4.5-q6k.gguf

Exit code: 0 iff every named model achieves the gate per category
(default gate: ≥80% PASS per category).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LLAMA_SERVER = REPO_ROOT / "models" / "llama.cpp" / "build" / "bin" / "llama-server"
FIXTURES_DIR = REPO_ROOT / "tests" / "fine_tune" / "fixtures"

# Same tools envelope as ab_multiturn.py so models behave consistently
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Run a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a file from disk.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents with ripgrep.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files by glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]
SYSTEM_PROMPT = (
    "You are Qwen, created by Alibaba Cloud. You are a helpful assistant "
    "with access to tools."
)

# llama-server on port 9099. Conflicts with ab_multiturn.py — run one at a time.
PORT = 9099
HOST = "127.0.0.1"
SERVER_BASE = f"http://{HOST}:{PORT}"

DEFAULT_MODELS = {
    "v1": REPO_ROOT / "models/gguf/qwen2.5-3b-toolcalls-q4km.gguf",
    "v4": REPO_ROOT / "models/gguf/qwen3-4b-toolcalls-v4-q6k.gguf",
}

DEFAULT_CATEGORIES = ["path_bias"]
DEFAULT_GATE = 0.80


def _start_server(gguf: Path, ctx: int = 4096) -> subprocess.Popen:
    """Start llama-server in background; wait for /v1/models to respond."""
    cmd = [
        str(LLAMA_SERVER), "-m", str(gguf),
        "-c", str(ctx), "--host", HOST, "--port", str(PORT),
        "--jinja", "--no-warmup",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{SERVER_BASE}/v1/models", timeout=2) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server died (exit={proc.returncode}) loading {gguf.name}")
        time.sleep(1)
    proc.terminate()
    raise TimeoutError(f"llama-server didn't respond in 90s for {gguf.name}")


def _stop_server(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=3)
    except ProcessLookupError:
        pass


def _chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 256) -> dict:
    payload = {
        "messages": messages, "tools": TOOLS,
        "temperature": temperature, "max_tokens": max_tokens, "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER_BASE}/v1/chat/completions",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _extract_args_text(resp: dict) -> tuple[str, list[dict]]:
    """Return (text_content, tool_call_dicts)."""
    if not resp.get("choices"):
        return "", []
    msg = resp["choices"][0].get("message", {}) or {}
    text = (msg.get("content") or "").strip()
    tc_list = msg.get("tool_calls") or []
    parsed_calls: list[dict] = []
    for tc in tc_list:
        fn = tc.get("function", {}) or {}
        name = fn.get("name")
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {"_raw": raw_args}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        parsed_calls.append({"name": name, "arguments": args})
    return text, parsed_calls


def _flatten_args_values(calls: list[dict]) -> str:
    """All arg values from all tool_calls, joined for substring matching."""
    parts: list[str] = []
    for c in calls:
        for v in (c.get("arguments") or {}).values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, (dict, list)):
                parts.append(json.dumps(v))
    return "\n".join(parts)


def score_scenario(scenario: dict, text: str, calls: list[dict]) -> tuple[bool, str]:
    """Deterministic scoring. Returns (passed, reason).

    PASS iff (no forbidden substring in args or text)
            AND (expected_path_in_args is None OR present in some args.value)
    """
    args_text = _flatten_args_values(calls)
    haystack = f"{text}\n{args_text}"

    # Forbidden substring check
    for forbidden in scenario.get("forbidden_substrings") or []:
        if forbidden in haystack:
            return False, f"FORBIDDEN substring '{forbidden}' found in model output"

    # Expected-path check
    expected = scenario.get("expected_path_in_args")
    if expected:
        if expected not in args_text:
            return False, f"EXPECTED path '{expected}' not present in any tool_call args"

    return True, "ok"


def run_category(model_name: str, gguf: Path, category: str) -> dict:
    fixture = FIXTURES_DIR / f"{category}_scenarios.jsonl"
    if not fixture.exists():
        return {"model": model_name, "category": category, "error": f"fixture missing: {fixture}"}

    scenarios = [json.loads(line) for line in fixture.open() if line.strip()]
    print(f"\n{'='*64}\nMODEL: {model_name}  CATEGORY: {category}  ({len(scenarios)} scenarios)\n{'='*64}")
    print(f"  starting llama-server on :{PORT}...")
    proc = _start_server(gguf)
    print("  server ready")

    trials: list[dict] = []
    counters: Counter = Counter()
    try:
        for sc in scenarios:
            sid = sc["id"]
            user = sc["user"]
            print(f"  [{sid}] {user[:80]}")
            msgs = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ]
            resp = _chat(msgs)
            text, calls = _extract_args_text(resp)
            passed, reason = score_scenario(sc, text, calls)
            counters["pass" if passed else "fail"] += 1
            status = "✓" if passed else "✗"
            print(f"     {status} {reason}")
            if not passed and calls:
                first_args = calls[0].get("arguments", {})
                print(f"       first_call: {calls[0].get('name')}({json.dumps(first_args)[:120]})")
            trials.append({
                "id": sid, "user": user,
                "expected_path_in_args": sc.get("expected_path_in_args"),
                "forbidden_substrings": sc.get("forbidden_substrings") or [],
                "model_text": text[:300],
                "model_calls": calls,
                "passed": passed, "reason": reason,
            })
    finally:
        print("  stopping server...")
        _stop_server(proc)

    n = len(scenarios)
    pass_n = counters["pass"]
    pass_rate = pass_n / max(n, 1)
    summary = {
        "model": model_name,
        "category": category,
        "n": n,
        "passed": pass_n,
        "failed": counters["fail"],
        "pass_rate": round(pass_rate, 3),
        "trials": trials,
    }
    print(f"\n  {model_name}/{category}: {pass_n}/{n} = {pass_rate:.0%}")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", nargs="+", default=None,
                   help="Model names from DEFAULT_MODELS. Default: v1, v4.")
    p.add_argument("--model", action="append", default=[],
                   help="Custom model: name=path/to.gguf (repeatable; overrides --models when used).")
    p.add_argument("--categories", nargs="+", default=DEFAULT_CATEGORIES,
                   help="Categories to run (each maps to <cat>_scenarios.jsonl in fixtures).")
    p.add_argument("--gate", type=float, default=DEFAULT_GATE,
                   help=f"Per-category pass-rate gate. Default {DEFAULT_GATE}.")
    p.add_argument("--out-dir", default=None)
    args = p.parse_args()

    if not LLAMA_SERVER.exists():
        print(f"FAIL: llama-server not at {LLAMA_SERVER}", file=sys.stderr)
        return 2

    if args.model:
        models: dict[str, Path] = {}
        for m in args.model:
            if "=" not in m:
                print(f"FAIL: --model must be name=path, got {m!r}", file=sys.stderr)
                return 2
            name, _, path = m.partition("=")
            models[name] = Path(path).resolve()
    else:
        names = args.models or list(DEFAULT_MODELS.keys())
        models = {n: DEFAULT_MODELS[n] for n in names if n in DEFAULT_MODELS}

    out_dir = Path(args.out_dir) if args.out_dir else (
        REPO_ROOT / "tests" / "fine_tune" / "runs"
        / f"eval-harder-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}")

    all_results: dict[str, dict[str, dict]] = defaultdict(dict)
    for model_name, gguf in models.items():
        if not gguf.exists():
            print(f"SKIP {model_name}: {gguf} not found")
            continue
        for category in args.categories:
            summary = run_category(model_name, gguf, category)
            all_results[model_name][category] = summary
            (out_dir / f"{model_name}_{category}.json").write_text(json.dumps(summary, indent=2))

    # Scoreboard
    lines = [
        "# Harder eval scoreboard",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Gate: pass_rate ≥ {args.gate:.0%} per category",
        "",
        "| Model | Category | N | Passed | Rate | Gate |",
        "|-------|----------|--:|-------:|-----:|:----:|",
    ]
    overall_pass = True
    for model_name, by_cat in all_results.items():
        for category, s in by_cat.items():
            ok = s["pass_rate"] >= args.gate
            overall_pass = overall_pass and ok
            status = "✓" if ok else "✗"
            lines.append(
                f"| {model_name} | {category} | {s['n']} | {s['passed']} | "
                f"{s['pass_rate']:.0%} | {status} |"
            )
    lines += ["", "## Failures (per model/category)", ""]
    for model_name, by_cat in all_results.items():
        for category, s in by_cat.items():
            fails = [t for t in s["trials"] if not t["passed"]]
            if fails:
                lines.append(f"### {model_name} / {category}")
                for t in fails:
                    lines.append(f"- **{t['id']}** — {t['reason']}")
                    lines.append(f"  - user: {t['user']!r}")
                    if t["model_calls"]:
                        first = t["model_calls"][0]
                        lines.append(f"  - first_call: {first['name']}({json.dumps(first['arguments'])[:120]})")
                    if t["model_text"]:
                        lines.append(f"  - text: {t['model_text'][:200]!r}")
                    lines.append("")

    (out_dir / "scoreboard.md").write_text("\n".join(lines))
    print("\n" + "\n".join(lines[:14]))
    print(f"\nFull scoreboard: {out_dir/'scoreboard.md'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
