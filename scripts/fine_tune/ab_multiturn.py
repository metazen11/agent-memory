#!/usr/bin/env python3
"""Multi-turn adaptation test for v1 / v3 / v4 via llama-server HTTP API.

REWRITE (2026-05-18): the prior llama-cli version corrupted outputs with
ANSI banner / spinner / ASCII-art / backspace control sequences mixed
into stdout. llama-cli b1-d132f22 prints these unconditionally even with
--log-disable --simple-io. The "v1 100% adaptation" result from that run
was a measurement artifact — the parser was scoring the llama-cli banner,
not the model's actual response.

This version uses llama-server. For each model:
  1. Start llama-server on a unique port with that model's GGUF
  2. Wait for /v1/models to respond
  3. For each scenario:
       Turn 1: POST /v1/chat/completions with [system, user]
       Turn 2: POST /v1/chat/completions with [system, user, assistant(turn1),
                                               tool(synthetic_response)]
       Compare tool_calls — same/different/text-only
  4. Stop the server, move to next model

Scoring:
  - regression_same_call: turn-2 emits same tool_call as turn-1 (BAD)
  - adapted_new_call:     turn-2 emits different tool_call (GOOD)
  - adapted_text_answer:  turn-2 emits text only, no tool_call (GOOD)
  - no_turn1_call:        model didn't call any tool on turn 1 (excluded)

Usage:
  .venv-finetune/bin/python scripts/fine_tune/ab_multiturn.py
  .venv-finetune/bin/python scripts/fine_tune/ab_multiturn.py --models v4
  .venv-finetune/bin/python scripts/fine_tune/ab_multiturn.py --prompts 10
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LLAMA_SERVER = REPO_ROOT / "models" / "llama.cpp" / "build" / "bin" / "llama-server"

MODELS = {
    "v1": REPO_ROOT / "models/gguf/qwen2.5-3b-toolcalls-q4km.gguf",
    "v3": REPO_ROOT / "models/gguf/qwen3-4b-toolcalls-v3-q6k.gguf",
    "v4": REPO_ROOT / "models/gguf/qwen3-4b-toolcalls-v4-q6k.gguf",
}

PORT = 9099  # confirmed free earlier; avoids LM Studio at 1234
HOST = "127.0.0.1"
SERVER_BASE = f"http://{HOST}:{PORT}"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Run a shell command and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."}
                },
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
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
]

SCENARIOS = [
    (
        "find the fire-map codebase",
        "/Users/mz/_CODING/fire-map.wfca.com",
    ),
    (
        "show me where auth is wired up",
        "app/auth.py\napp/middleware/auth_middleware.py\napp/api/login.py",
    ),
    (
        "locate the migrations directory",
        "scripts/migrations/\n001-initial.sql\n012-prompt-toolcall-linkage.sql",
    ),
    (
        "what tests cover the backfill",
        "tests/migrations/test_012.py\ntests/test_backfill_jsonl.py",
    ),
    (
        "open the runbook",
        "docs/fine_tune/PIPELINE_RUNBOOK.md\ndocs/fine_tune/WIZARD.md",
    ),
    (
        "show me the dataset builder",
        "scripts/fine_tune/build_v3_dataset.py\nscripts/fine_tune/build_v4_dataset.py",
    ),
    (
        "where is the database schema",
        "scripts/init_db.sql\nscripts/migrations/",
    ),
    (
        "find the README",
        "/Users/mz/_CODING/agentMemory/README.md",
    ),
    (
        "look at the API routes",
        "app/routes/observations.py\napp/routes/lessons.py\napp/routes/prompts.py",
    ),
    (
        "show me the hooks",
        "hooks/user-prompt-submit.js\nhooks/post-tool-use.js\nhooks/session-start.js",
    ),
]

SYSTEM_PROMPT = (
    "You are Qwen, created by Alibaba Cloud. You are a helpful assistant "
    "with access to tools."
)


def _start_server(gguf: Path, ctx: int = 4096) -> subprocess.Popen:
    """Start llama-server in background; return Popen handle."""
    cmd = [
        str(LLAMA_SERVER),
        "-m", str(gguf),
        "-c", str(ctx),
        "--host", HOST,
        "--port", str(PORT),
        "--jinja",
        "--no-warmup",
    ]
    # Suppress server's chatty stdout/stderr to /dev/null — we only care
    # about the HTTP API. Logs are routed through llama-server itself.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    # Wait for /v1/models to respond (model load can take 5-20s)
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{SERVER_BASE}/v1/models")
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            pass
        # If proc died, fail loud
        if proc.poll() is not None:
            raise RuntimeError(
                f"llama-server died (exit={proc.returncode}) loading {gguf.name}"
            )
        time.sleep(1)
    proc.terminate()
    raise TimeoutError(f"llama-server didn't respond in 90s for {gguf.name}")


def _stop_server(proc: subprocess.Popen) -> None:
    """Stop llama-server cleanly; SIGKILL after 5s if needed."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=3)
    except ProcessLookupError:
        pass


def _chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 256) -> dict:
    """POST to /v1/chat/completions; return parsed JSON response."""
    payload = {
        "messages": messages,
        "tools": TOOLS,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SERVER_BASE}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _extract_call(resp: dict) -> tuple[dict | None, str]:
    """Return (parsed_tool_call, text_content). Either may be empty."""
    if not resp.get("choices"):
        return None, ""
    msg = resp["choices"][0].get("message", {}) or {}
    text = (msg.get("content") or "").strip()
    tc_list = msg.get("tool_calls") or []
    if not tc_list:
        return None, text
    tc = tc_list[0]
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
    return {"name": name, "arguments": args}, text


def _calls_equal(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return False
    return a.get("name") == b.get("name") and a.get("arguments") == b.get("arguments")


def run_scenarios(model_name: str, gguf: Path, scenarios: list[tuple[str, str]]) -> dict:
    print(f"\n{'='*60}\nMODEL: {model_name} ({gguf.name})\n{'='*60}")
    print(f"  starting llama-server on :{PORT}...")
    proc = _start_server(gguf)
    print("  server ready")
    out: list[dict] = []
    counters: Counter = Counter()
    try:
        for i, (user, tool_response) in enumerate(scenarios, 1):
            print(f"  [{i}/{len(scenarios)}] {user}")
            t1_msgs = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ]
            t1_resp = _chat(t1_msgs)
            t1_call, t1_text = _extract_call(t1_resp)

            if t1_call is None:
                counters["turn1_no_tool_call"] += 1
                print(f"     turn1: NO tool_call. text={t1_text[:80]!r}")
                out.append({
                    "i": i, "user": user,
                    "turn1_text": t1_text,
                    "turn1_tool_call": None,
                    "turn2_skipped": True,
                })
                continue
            print(f"     turn1: {t1_call['name']}({json.dumps(t1_call['arguments'])[:90]})")

            # Turn 2: append assistant(tool_call) + tool(synthetic_response)
            t2_msgs = t1_msgs + [
                {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": t1_call["name"],
                            "arguments": json.dumps(t1_call["arguments"]),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": tool_response,
                },
            ]
            t2_resp = _chat(t2_msgs)
            t2_call, t2_text = _extract_call(t2_resp)

            if t2_call is None:
                if t2_text:
                    counters["adapted_text_answer"] += 1
                    verdict = "✓ adapted (text answer)"
                else:
                    counters["empty_response"] += 1
                    verdict = "? empty response"
            elif _calls_equal(t1_call, t2_call):
                counters["regression_same_call"] += 1
                verdict = "✗ REGRESSION (re-emitted identical tool_call)"
            else:
                counters["adapted_new_call"] += 1
                verdict = "✓ adapted (different tool_call)"
            print(f"     turn2: {verdict}")
            if t2_call:
                print(f"           {t2_call['name']}({json.dumps(t2_call['arguments'])[:90]})")
            elif t2_text:
                print(f"           text={t2_text[:120]!r}")

            out.append({
                "i": i, "user": user,
                "turn1_text": t1_text,
                "turn1_tool_call": t1_call,
                "synthetic_tool_response": tool_response[:200],
                "turn2_text": t2_text,
                "turn2_tool_call": t2_call,
                "verdict": verdict,
            })
    finally:
        print("  stopping server...")
        _stop_server(proc)

    n = len(scenarios)
    adapted = counters["adapted_text_answer"] + counters["adapted_new_call"]
    no_call = counters["turn1_no_tool_call"]
    empty = counters["empty_response"]
    eligible = n - no_call - empty
    summary = {
        "model": model_name,
        "n_scenarios": n,
        "n_no_turn1_tool_call": no_call,
        "n_empty_turn2_response": empty,
        "n_regression_same_call": counters["regression_same_call"],
        "n_adapted_new_call": counters["adapted_new_call"],
        "n_adapted_text_answer": counters["adapted_text_answer"],
        "adaptation_rate_of_eligible": round(adapted / max(eligible, 1), 3),
        "trials": out,
    }
    print(f"\n  Adaptation rate (eligible): {summary['adaptation_rate_of_eligible']:.0%}  "
          f"({adapted}/{eligible})")
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", nargs="+", default=["v1", "v3", "v4"],
                   choices=list(MODELS.keys()))
    p.add_argument("--prompts", type=int, default=len(SCENARIOS),
                   help=f"Number of scenarios to run (max {len(SCENARIOS)}).")
    p.add_argument("--out-dir", default=None)
    args = p.parse_args()

    if not LLAMA_SERVER.exists():
        print(f"FAIL: llama-server not at {LLAMA_SERVER}", file=sys.stderr)
        return 2

    scenarios = SCENARIOS[: args.prompts]
    out_dir = Path(args.out_dir) if args.out_dir else (
        REPO_ROOT / "tests" / "fine_tune" / "runs"
        / f"ab-mt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output dir: {out_dir}")

    all_summaries: dict[str, dict] = {}
    for model in args.models:
        gguf = MODELS[model]
        if not gguf.exists():
            print(f"SKIP {model}: {gguf} not found")
            continue
        summary = run_scenarios(model, gguf, scenarios)
        all_summaries[model] = summary
        (out_dir / f"{model}.json").write_text(json.dumps(summary, indent=2))

    sb = [
        "# Multi-turn adaptation scoreboard",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Scenarios: {len(scenarios)}, harness: llama-server :{PORT}",
        "",
        "| Model | N | NoTurn1Call | EmptyTurn2 | Regression | NewCall | TextAns | Adaptation |",
        "|-------|--:|------------:|-----------:|-----------:|--------:|--------:|-----------:|",
    ]
    for m, s in all_summaries.items():
        sb.append(
            f"| {m} | {s['n_scenarios']} | "
            f"{s['n_no_turn1_tool_call']} | "
            f"{s['n_empty_turn2_response']} | "
            f"{s['n_regression_same_call']} | "
            f"{s['n_adapted_new_call']} | "
            f"{s['n_adapted_text_answer']} | "
            f"{s['adaptation_rate_of_eligible']:.0%} |"
        )
    sb += [
        "",
        "## Ship gate",
        "- adaptation_rate ≥ 60% AND regression_same_call ≤ floor(N/5)",
        "",
    ]
    (out_dir / "scoreboard.md").write_text("\n".join(sb))
    print("\n" + "\n".join(sb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
