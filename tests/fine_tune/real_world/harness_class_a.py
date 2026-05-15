#!/usr/bin/env python3
"""Class A — in-distribution replay harness.

For each of N sampled sessions from valid.chat.jsonl:
  - Build a chat-completions request with the source system+user only.
  - Pass the 5 default trained tools (Bash, Read, Write, Grep, Edit) as `tools`.
  - Get the model's turn-1 emission.
  - Compare to the actual next assistant turn (msg index 2 in the source):
      shape_match    same tool name + same set of arg keys
      close_match    same tool name + >= 50% arg-value overlap
      needs_review   different tool name (could be plausible alternative)
      wrong          text response when training had tool_call, or vice versa

Emits an eval-report.schema.json conforming JSON.

Usage:
    python harness_class_a.py <base_url> <model> <model_id> <quant> <out.json> \
        [--n 30] [--seed 17] [--report-id ID]
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


# ------ tool schema loading -------------------------------------------------

def load_default_tool_schemas() -> list[dict]:
    """Return OpenAI-envelope tool list for the 5 default trained tools."""
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


# ------ session sampling ----------------------------------------------------

def sample_sessions(n: int, seed: int, require_default_tool: bool = True) -> list[dict]:
    """Read valid.chat.jsonl and return a random sample of n parsed sessions.

    Filters to sessions where the assistant's first tool_call name is in
    DEFAULT_TRAINED_TOOLS, so we're testing in-distribution behavior."""
    rng = random.Random(seed)
    sessions = []
    with VALID.open() as f:
        for line in f:
            sessions.append(json.loads(line))
    if require_default_tool:
        keep = []
        for s in sessions:
            msgs = s.get("messages") or []
            tc = None
            for m in msgs:
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    tc = m["tool_calls"][0]
                    break
            if tc and tc.get("function", {}).get("name") in DEFAULT_TRAINED_TOOLS:
                keep.append(s)
        sessions = keep
    rng.shuffle(sessions)
    return sessions[:n]


# ------ chat completion -----------------------------------------------------

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


# ------ comparison ----------------------------------------------------------

def classify(model_call: dict | None, model_text: str, gold_call: dict | None) -> tuple[str, dict]:
    """Compare model output to gold next-assistant-turn.

    Returns (verdict, details).
    """
    details: dict[str, Any] = {}
    if gold_call is None:
        # ground truth was a text reply; only "wrong" if model emitted a tool_call
        if model_call is None:
            return "shape_match", {"reason": "both text"}
        return "wrong", {"reason": "training had text reply, model emitted tool_call",
                         "model_tool": model_call["name"]}

    if model_call is None:
        return "wrong", {"reason": "training had tool_call, model emitted text",
                         "gold_tool": gold_call["name"], "model_text_preview": model_text[:160]}

    gold_name = gold_call.get("name") or ""
    model_name = model_call.get("name") or ""
    gold_args = gold_call.get("arguments") or {}
    if not isinstance(gold_args, dict):
        gold_args = {}
    model_args = model_call.get("arguments") or {}
    if not isinstance(model_args, dict):
        model_args = {}

    details.update({"gold_tool": gold_name, "model_tool": model_name,
                    "gold_args_keys": sorted(gold_args.keys()),
                    "model_args_keys": sorted(model_args.keys())})

    if model_name != gold_name:
        return "needs_review", {**details, "reason": "different tool name"}

    # same tool name → compare arg keys
    gold_keys = set(gold_args.keys())
    model_keys = set(model_args.keys())
    if gold_keys == model_keys:
        return "shape_match", details

    # same tool, partial overlap → close_match by value match count
    overlap_keys = gold_keys & model_keys
    if overlap_keys:
        same_vals = sum(1 for k in overlap_keys if str(gold_args.get(k)) == str(model_args.get(k)))
        denom = max(len(gold_keys), 1)
        if same_vals / denom >= 0.5:
            return "close_match", {**details, "value_match_rate": round(same_vals / denom, 2)}
    return "needs_review", {**details, "reason": "arg keys differ"}


# ------ main ----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("model")
    ap.add_argument("model_id")
    ap.add_argument("quant")
    ap.add_argument("out_path")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--report-id", default=None)
    ap.add_argument("--model-path", default=None)
    args = ap.parse_args()

    tools = load_default_tool_schemas()
    sessions = sample_sessions(args.n, args.seed)
    print(f"sampled {len(sessions)} sessions (seed={args.seed})", flush=True)

    prompts_out = []
    counts = {"shape_match": 0, "close_match": 0, "needs_review": 0, "wrong": 0, "error": 0}

    for i, sess in enumerate(sessions, 1):
        msgs = sess["messages"]
        system = msgs[0]["content"] or "You are Qwen, created by Alibaba Cloud. You are a helpful assistant with access to tools."
        user_text = msgs[1]["content"] or ""
        # gold = first assistant message after the user (only tool_call shape used downstream)
        gold_call = None
        for m in msgs[2:]:
            if m.get("role") == "assistant":
                if m.get("tool_calls"):
                    fn = m["tool_calls"][0]["function"]
                    gold_call = {"name": fn["name"], "arguments": fn.get("arguments") or {}}
                break

        chat_msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
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
            verdict, details = classify(model_call, model_text, gold_call)
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

        latency = round(time.time() - t0, 2)
        counts[verdict] = counts.get(verdict, 0) + 1
        print(f"  [{i}/{len(sessions)}] {verdict} ({latency}s)  user={user_text[:60]!r}", flush=True)

        notes = json.dumps(details, ensure_ascii=False)[:500]
        first_tool_call = None
        if model_call:
            first_tool_call = {"name": model_call["name"], "arguments": model_call["arguments"]}

        prompts_out.append({
            "id": i,
            "text": user_text,
            "expected_intent": f"tool_call:{gold_call['name']}" if gold_call else "text_reply",
            "outcome": _verdict_to_outcome(verdict),
            "n_turns": 1,
            "total_tokens": tokens,
            **({"first_tool_call": first_tool_call} if first_tool_call else {}),
            "notes": notes,
            "regressions": [verdict] if verdict in {"wrong", "needs_review"} else [],
        })

    n = sum(counts.values())
    shape_rate = counts["shape_match"] / n if n else 0.0
    shape_or_close = (counts["shape_match"] + counts["close_match"]) / n if n else 0.0

    report = {
        "report_id": args.report_id or f"class-a-{args.model_id}-{date.today().isoformat()}",
        "run_date": date.today().isoformat(),
        "model": {
            "id": args.model_id,
            "path": args.model_path or "",
            "quant": args.quant,
            "params": "3B",
        },
        "harness": {
            "name": "harness_class_a.py",
            "version": "phase-0.5-baseline-20260515",
            "endpoint": "/v1/chat/completions",
            "server": f"llama-server --jinja -c 8192 ({args.base_url})",
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "max_turns": 1,
        },
        "eval_class": "A",
        "gates": [
            {
                "name": "shape_match rate",
                "threshold": ">= 70",
                "actual": round(shape_rate * 100, 1),
                "units": "%",
                "pass": shape_rate >= 0.70,
                "notes": f"{counts['shape_match']}/{n} sessions match tool name + arg keys exactly",
            },
            {
                "name": "shape_or_close rate",
                "threshold": ">= 80",
                "actual": round(shape_or_close * 100, 1),
                "units": "%",
                "pass": shape_or_close >= 0.80,
                "notes": f"{counts['shape_match'] + counts['close_match']}/{n} shape+close",
            },
            {
                "name": "wrong rate",
                "threshold": "<= 10",
                "actual": round(counts["wrong"] / n * 100, 1) if n else 0,
                "units": "%",
                "pass": (counts["wrong"] / n) <= 0.10 if n else True,
                "notes": f"{counts['wrong']}/{n} model gave text when training had tool_call (or vice versa)",
            },
        ],
        "prompts": prompts_out,
        "aggregate_stats": {
            "shape_match": f"{counts['shape_match']}/{n}",
            "close_match": f"{counts['close_match']}/{n}",
            "needs_review": f"{counts['needs_review']}/{n}",
            "wrong": f"{counts['wrong']}/{n}",
            "error": f"{counts.get('error', 0)}/{n}",
            "shape_match_rate_pct": round(shape_rate * 100, 1),
            "shape_or_close_rate_pct": round(shape_or_close * 100, 1),
            "sample_seed": args.seed,
        },
        "notable_findings": (
            "In-distribution replay: for each session, the model is given the source system+user "
            "and the 5 default trained tools, and its turn-1 emission is compared to the gold "
            "next-assistant turn from the training data. `shape_match` requires identical tool "
            "name + identical arg key set; `close_match` allows partial key overlap with >=50% "
            "value match; `needs_review` flags different tool names (could be a plausible "
            "alternative); `wrong` flags type mismatch (text vs tool_call)."
        ),
        "verdict": {
            "pass": shape_rate >= 0.70,
            "headline": (
                f"Class A in-distribution replay: {counts['shape_match']}/{n} "
                f"shape_match ({shape_rate * 100:.0f}%), "
                f"{counts['close_match']}/{n} close, "
                f"{counts['needs_review']}/{n} review, "
                f"{counts['wrong']}/{n} wrong."
            ),
            "recommendation": (
                "Use this as v3 baseline for in-distribution recall. Inspect `needs_review` rows "
                "manually to decide which alternative tool choices are acceptable. Drift versus "
                "training data is unexpected and indicates either training/eval template "
                "mismatch or undertrained tool selection."
            ),
        },
        "artifacts": [
            {"name": "raw transcripts", "path": args.out_path},
        ],
    }

    Path(args.out_path).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out_path}", flush=True)
    print(f"counts: {counts}  shape_rate={shape_rate:.1%}", flush=True)
    return 0


def _verdict_to_outcome(v: str) -> str:
    """Map our verdicts to the schema's enum.

    The schema's `outcome` enum includes the Class B values
    (adapted_tool_call/text_answer/identical_reemit/off_topic) but not
    'shape_match'/'close_match'/etc. We pick the closest existing value:
      shape_match  -> useful_answer
      close_match  -> useful_answer
      needs_review -> off_topic   (different tool — plausible alt)
      wrong        -> text_only_fallback   (text where tool expected) or off_topic
      error        -> error
    """
    return {
        "shape_match": "useful_answer",
        "close_match": "useful_answer",
        "needs_review": "off_topic",
        "wrong": "text_only_fallback",
        "error": "error",
    }.get(v, "off_topic")


if __name__ == "__main__":
    sys.exit(main())
