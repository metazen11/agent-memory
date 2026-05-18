#!/usr/bin/env python3
"""Chat-completions harness for v2 GGUF retest.

Same 10 prompts, same 5 tools, same fake_tool_result, same loop detector,
same outcome classification as tests/fine_tune/real_world/harness.py.
Difference: uses llama-server /v1/chat/completions with --jinja (model's
own chat template), and OpenAI-format messages including a proper tool role.

This isolates whether the previous v2 regression was a hand-rolled-ChatML
artifact or a genuine model regression.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Import shared logic from the existing harness so we cannot drift on the
# fake_tool_result / loop / analysis logic.
REPO = Path("/Users/mz/_CODING/agentMemory")
sys.path.insert(0, str(REPO / "tests" / "fine_tune" / "real_world"))
from harness import (  # type: ignore
    PROMPTS,
    load_schemas,
    fake_tool_result,
    detect_loop,
    analyze_session,
)


def chat_completion(base_url: str, messages: list[dict], tools: list[dict],
                    temperature: float, max_tokens: int, model: str) -> dict:
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


def extract_call(message: dict) -> dict | None:
    """Convert OpenAI tool_calls[0] into the {name, arguments} shape used by the rest of the pipeline."""
    tcs = message.get("tool_calls") or []
    if not tcs:
        return None
    tc = tcs[0]
    fn = tc.get("function") or {}
    name = fn.get("name") or ""
    raw_args = fn.get("arguments")
    # arguments can be a JSON string (OpenAI) or already a dict
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {"_raw": raw_args}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    return {"name": name, "arguments": args, "_id": tc.get("id") or "call_1"}


def run_session(base_url: str, model: str, prompt_text: str, tools: list[dict],
                temperature: float = 0.2, max_turns: int = 5, max_tokens: int = 512) -> dict:
    messages: list[dict] = [
        {"role": "system",
         "content": "You are Qwen, created by Alibaba Cloud. You are a helpful assistant with access to tools."},
        {"role": "user", "content": prompt_text},
    ]
    transcript = []
    total_tokens = 0
    parsed_calls_seq: list[dict] = []
    outcome = "unknown"

    for turn_idx in range(max_turns):
        t0 = time.time()
        try:
            resp = chat_completion(base_url, messages, tools, temperature, max_tokens, model)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:600]
            except Exception:
                pass
            transcript.append({"turn": turn_idx + 1, "error": f"http {e.code}: {body}"})
            outcome = "error"
            break
        except urllib.error.URLError as e:
            transcript.append({"turn": turn_idx + 1, "error": f"url error: {e}"})
            outcome = "error"
            break
        latency = time.time() - t0

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        finish_reason = choice.get("finish_reason")
        usage = resp.get("usage") or {}
        tokens_predicted = usage.get("completion_tokens") or 0
        total_tokens += int(tokens_predicted)

        call = extract_call(msg)
        transcript.append({
            "turn": turn_idx + 1,
            "content": content,
            "tool_call": {"name": call["name"], "arguments": call["arguments"]} if call else None,
            "finish_reason": finish_reason,
            "latency_s": round(latency, 2),
            "tokens_predicted": tokens_predicted,
        })

        if call is None:
            outcome = "text_response"
            messages.append({"role": "assistant", "content": content})
            break

        parsed_calls_seq.append({"name": call["name"], "arguments": call["arguments"]})
        if detect_loop(parsed_calls_seq):
            outcome = "looped"
            messages.append({"role": "assistant", "content": content or None,
                             "tool_calls": [{"id": call["_id"], "type": "function",
                                             "function": {"name": call["name"],
                                                          "arguments": json.dumps(call["arguments"])}}]})
            break

        fake = fake_tool_result({"name": call["name"], "arguments": call["arguments"]}, prompt_text)
        # Append assistant tool_call then tool result, OpenAI-compatible
        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": [{"id": call["_id"], "type": "function",
                                         "function": {"name": call["name"],
                                                      "arguments": json.dumps(call["arguments"])}}]})
        messages.append({"role": "tool", "tool_call_id": call["_id"], "content": fake})
        transcript[-1]["fake_tool_result"] = fake

    else:
        outcome = "max_turns"

    return {
        "prompt": prompt_text,
        "outcome": outcome,
        "turns": transcript,
        "total_tokens": total_tokens,
        "parsed_calls": parsed_calls_seq,
    }


def adapt_session_for_analysis(s: dict) -> dict:
    """analyze_session expects each turn to have key 'raw' (parsed by regex).
    Our chat turns store 'content'+'tool_call' instead; rebuild minimal shape.
    """
    fake = dict(s)
    fake["turns"] = []
    for tr in s["turns"]:
        fake["turns"].append({
            "turn": tr.get("turn"),
            "tool_call": tr.get("tool_call"),
            "raw": tr.get("content", ""),
        })
    return fake


def main():
    if len(sys.argv) < 4:
        print("usage: harness_chat.py <base_url> <model> <out.json>", file=sys.stderr)
        sys.exit(2)
    base_url, model, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    schemas = load_schemas()
    results = []
    for i, p in enumerate(PROMPTS, 1):
        print(f"[{base_url}] prompt {i}/{len(PROMPTS)}: {p[:60]}", flush=True)
        sess = run_session(base_url, model, p, schemas)
        sess["analysis"] = analyze_session(adapt_session_for_analysis(sess))
        results.append(sess)
        a = sess["analysis"]
        print(f"   -> outcome={a['final_outcome']} turns={a['n_turns']} tokens={sess['total_tokens']}", flush=True)
    Path(out_path).write_text(json.dumps(results, indent=2))
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
