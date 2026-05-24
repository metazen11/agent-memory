#!/usr/bin/env python3
"""Class B — tool_response adaptation harness.

For N sampled sessions from valid.chat.jsonl with structure
  system → user → assistant(tool_calls) → tool
feed the model the first 3 turns (user + assistant tool_call + REAL tool response)
and observe its turn-4 emission:

  adapted_tool_call  different tool name OR different args than turn-2
  text_answer        empty tool_calls, non-empty content
  identical_reemit   same name AND identical args (the v2 bug)
  off_topic          different but unrelated (different name w/ no overlap or empty)

adaptation_rate = (adapted_tool_call + text_answer) / total

Emits an eval-report.schema.json conforming JSON.

Usage:
    python harness_class_b.py <base_url> <model> <model_id> <quant> <out.json> \
        [--n 30] [--seed 23] [--report-id ID]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path("/Users/mz/_CODING/agentMemory")
VALID = REPO / "data" / "processed" / "qwen25_tools" / "v2" / "valid.chat.jsonl"
SCHEMAS_FILE = REPO / "data" / "processed" / "qwen25_tools" / "v2" / "tool_schemas.json"
DEFAULT_TRAINED_TOOLS = ["Bash", "Read", "Write", "Grep", "Edit"]
MAX_TOOL_CONTENT = 4000  # truncate huge persisted-output blobs to keep ctx window sane


def load_default_tool_schemas() -> list[dict]:
    reg = json.loads(SCHEMAS_FILE.read_text())
    out = []
    for name in DEFAULT_TRAINED_TOOLS:
        sch = reg[name]
        description = sch.get("description") or f"Tool '{name}'."
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": sch.get("properties", {}),
                    "required": sch.get("required", []),
                },
            },
        })
    return out


def sample_sessions(n: int, seed: int) -> list[dict]:
    """Return sessions where the first assistant uses a default trained tool
    and a tool response follows."""
    rng = random.Random(seed)
    out = []
    with VALID.open() as f:
        for line in f:
            s = json.loads(line)
            msgs = s.get("messages") or []
            if len(msgs) < 4:
                continue
            if msgs[1].get("role") != "user":
                continue
            a = msgs[2]
            if a.get("role") != "assistant" or not a.get("tool_calls"):
                continue
            name = a["tool_calls"][0].get("function", {}).get("name", "")
            if name not in DEFAULT_TRAINED_TOOLS:
                continue
            if msgs[3].get("role") != "tool":
                continue
            out.append(s)
    rng.shuffle(out)
    return out[:n]


def chat_completion(base_url: str, model: str, messages: list[dict], tools: list[dict],
                    temperature: float, max_tokens: int) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read())


def extract_tool_call(message: dict) -> dict | None:
    tcs = message.get("tool_calls") or []
    if not tcs:
        return None
    fn = (tcs[0].get("function") or {})
    name = fn.get("name") or ""
    raw_args = fn.get("arguments")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    return {"name": name, "arguments": args}


def classify(model_call: dict | None, model_text: str, prior_call: dict) -> tuple[str, dict]:
    prior_name = prior_call.get("name") or ""
    prior_args = prior_call.get("arguments") or {}
    if not isinstance(prior_args, dict):
        prior_args = {}
    details: dict[str, Any] = {"prior_tool": prior_name, "prior_args_keys": sorted(prior_args.keys())}

    if model_call is None:
        text = model_text.strip()
        if not text:
            return "off_topic", {**details, "reason": "empty text and no tool_call"}
        details["text_preview"] = text[:160]
        return "text_answer", details

    name = model_call.get("name") or ""
    args = model_call.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    details.update({"model_tool": name, "model_args_keys": sorted(args.keys())})

    if name == prior_name and args == prior_args:
        return "identical_reemit", details

    if name == prior_name:
        # different args, same tool → adapted
        return "adapted_tool_call", {**details, "reason": "same tool, different args"}

    # different tool → still adapted (the model decided something else was needed)
    return "adapted_tool_call", {**details, "reason": "different tool"}


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "...[truncated]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("model")
    ap.add_argument("model_id")
    ap.add_argument("quant")
    ap.add_argument("out_path")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--report-id", default=None)
    ap.add_argument("--model-path", default=None)
    args = ap.parse_args()

    tools = load_default_tool_schemas()
    sessions = sample_sessions(args.n, args.seed)
    print(f"sampled {len(sessions)} sessions (seed={args.seed})", flush=True)

    counts = {"adapted_tool_call": 0, "text_answer": 0, "identical_reemit": 0,
              "off_topic": 0, "error": 0}
    prompts_out = []

    for i, sess in enumerate(sessions, 1):
        msgs = sess["messages"]
        system = msgs[0].get("content") or "You are Qwen, created by Alibaba Cloud. You are a helpful assistant with access to tools."
        user_text = msgs[1].get("content") or ""
        gold_a = msgs[2]
        tool_msg = msgs[3]
        gold_fn = gold_a["tool_calls"][0]["function"]
        prior_call = {"name": gold_fn["name"], "arguments": gold_fn.get("arguments") or {}}
        tool_content = _truncate(tool_msg.get("content") or "", MAX_TOOL_CONTENT)

        call_id = "call_b1"
        chat_msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": prior_call["name"],
                        "arguments": json.dumps(prior_call["arguments"]),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": tool_content},
        ]

        t0 = time.time()
        verdict = "error"
        details: dict[str, Any] = {}
        model_call = None
        model_text = ""
        tokens = 0
        try:
            resp = chat_completion(args.base_url, args.model, chat_msgs, tools,
                                   args.temperature, args.max_tokens)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            model_text = msg.get("content") or ""
            model_call = extract_tool_call(msg)
            usage = resp.get("usage") or {}
            tokens = int(usage.get("completion_tokens") or 0)
            verdict, details = classify(model_call, model_text, prior_call)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:600]
            except Exception:
                pass
            details = {"error": f"http {e.code}: {body}"}
        except urllib.error.URLError as e:
            details = {"error": f"url error: {e}"}
        except Exception as e:  # noqa: BLE001
            details = {"error": f"unexpected: {type(e).__name__}: {e}"}

        counts[verdict] = counts.get(verdict, 0) + 1
        latency = round(time.time() - t0, 2)
        print(f"  [{i}/{len(sessions)}] {verdict} ({latency}s) prior={prior_call['name']}", flush=True)

        notes = json.dumps(details, ensure_ascii=False)[:500]
        first_tool_call = None
        if model_call:
            first_tool_call = {"name": model_call["name"], "arguments": model_call["arguments"]}

        prompts_out.append({
            "id": i,
            "text": user_text,
            "expected_intent": f"adapt_after:{prior_call['name']}",
            "outcome": verdict,
            "n_turns": 4,
            "total_tokens": tokens,
            **({"first_tool_call": first_tool_call} if first_tool_call else {}),
            "notes": notes,
            "regressions": [verdict] if verdict == "identical_reemit" else [],
        })

    n = sum(counts.values())
    adapt_rate = (counts["adapted_tool_call"] + counts["text_answer"]) / n if n else 0.0
    reemit_rate = counts["identical_reemit"] / n if n else 0.0

    report = {
        "report_id": args.report_id or f"class-b-{args.model_id}-{date.today().isoformat()}",
        "run_date": date.today().isoformat(),
        "model": {
            "id": args.model_id,
            "path": args.model_path or "",
            "quant": args.quant,
            "params": "3B",
        },
        "harness": {
            "name": "harness_class_b.py",
            "version": "phase-0.5-baseline-20260515",
            "endpoint": "/v1/chat/completions",
            "server": f"llama-server --jinja -c 8192 ({args.base_url})",
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "max_turns": 1,
        },
        "eval_class": "B",
        "gates": [
            {
                "name": "adaptation rate",
                "threshold": ">= 70",
                "actual": round(adapt_rate * 100, 1),
                "units": "%",
                "pass": adapt_rate >= 0.70,
                "notes": f"(adapted+text)/total = {counts['adapted_tool_call']}+{counts['text_answer']}/{n}",
            },
            {
                "name": "identical_reemit rate",
                "threshold": "<= 20",
                "actual": round(reemit_rate * 100, 1),
                "units": "%",
                "pass": reemit_rate <= 0.20,
                "notes": f"{counts['identical_reemit']}/{n} sessions re-emitted exactly the prior call (v2 loop bug)",
            },
            {
                "name": "text_answer rate",
                "threshold": ">= 30",
                "actual": round(counts["text_answer"] / n * 100, 1) if n else 0,
                "units": "%",
                "pass": (counts["text_answer"] / n) >= 0.30 if n else False,
                "notes": f"{counts['text_answer']}/{n} synthesized a final text reply after seeing the tool result",
            },
        ],
        "prompts": prompts_out,
        "aggregate_stats": {
            "adapted_tool_call": f"{counts['adapted_tool_call']}/{n}",
            "text_answer": f"{counts['text_answer']}/{n}",
            "identical_reemit": f"{counts['identical_reemit']}/{n}",
            "off_topic": f"{counts['off_topic']}/{n}",
            "error": f"{counts['error']}/{n}",
            "adaptation_rate_pct": round(adapt_rate * 100, 1),
            "identical_reemit_pct": round(reemit_rate * 100, 1),
            "sample_seed": args.seed,
        },
        "notable_findings": (
            "Class B feeds the model the first 3 turns of a real session (user + the gold "
            "assistant tool_call + the REAL tool response from training data) and looks at "
            "the turn-4 emission. `identical_reemit` is the v2 regression signal: the model "
            "re-emits exactly the same tool_call instead of either adapting or synthesizing a "
            "text answer. `adapted_tool_call` (different args, or different tool) + `text_answer` "
            "both count as healthy adaptation."
        ),
        "verdict": {
            "pass": adapt_rate >= 0.70 and reemit_rate <= 0.20,
            "headline": (
                f"Class B adaptation: {adapt_rate * 100:.0f}% adapt-or-answer, "
                f"{reemit_rate * 100:.0f}% identical re-emit, "
                f"text_answer {counts['text_answer']}/{n}."
            ),
            "recommendation": (
                "Compare adaptation_rate and identical_reemit between v1 and v2 to confirm the "
                "v2 regression hypothesis (training-data imbalance toward tool_call-terminal "
                "turns). v3's training data must include more (user → tc → tool → text-answer) "
                "completions, and the identical_reemit gate should drop below 20%."
            ),
        },
        "artifacts": [
            {"name": "raw transcripts", "path": args.out_path},
        ],
    }

    Path(args.out_path).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out_path}", flush=True)
    print(f"counts: {counts}  adapt_rate={adapt_rate:.1%}  reemit={reemit_rate:.1%}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
