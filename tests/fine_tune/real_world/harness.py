#!/usr/bin/env python3
"""Real-world A/B test harness for v1 vs v2 tool-call GGUF.

Multi-turn agentic sessions:
  - up to 5 turns
  - each assistant turn parsed for <tool_call>; if found, harness fakes
    a plausible tool result and feeds it back as a tool message
  - stops on first text-only response, or after 5 turns
  - records per-turn raw output, parsed calls, fake results, total tokens

Uses llama.cpp llama-server /completion endpoint with explicit ChatML
prompts (matches validator's _build_prompt_for_llama_cli format).
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path("/Users/mz/_CODING/agentMemory")
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def load_schemas() -> list[dict]:
    """Load v2 schemas for the 5 default trained tools, in OpenAI envelope shape."""
    schemas_file = REPO / "data" / "processed" / "qwen25_tools" / "v2" / "tool_schemas.json"
    registry = json.loads(schemas_file.read_text())
    keep = ["Bash", "Read", "Write", "Grep", "Edit"]
    out = []
    for name in keep:
        sch = registry[name]
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


def build_system_block(schemas: list[dict]) -> str:
    tools_json = "\n".join(json.dumps(s) for s in schemas)
    return (
        "<|im_start|>system\n"
        "You are Qwen, created by Alibaba Cloud. You are a helpful assistant with access to tools.\n\n"
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        f"<tools>\n{tools_json}\n</tools>\n\n"
        "For each function call, return a json object with function name and arguments within "
        "<tool_call></tool_call> XML tags:\n"
        '<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>'
        "<|im_end|>\n"
    )


def build_prompt(system_block: str, turns: list[dict]) -> str:
    """Concatenate ChatML turns to a single prompt string.

    Each turn: {"role": "user"|"assistant"|"tool", "content": "..."}
    Assistant content already raw (may include <tool_call>...).
    Tool content wrapped as <tool_response>...</tool_response> inside a user turn,
    which matches Hermes/Qwen2.5 tool-message convention.
    """
    out = [system_block]
    for t in turns:
        role = t["role"]
        content = t["content"]
        if role == "tool":
            # Qwen2.5 + Hermes convention: tool responses come back inside a
            # user turn wrapped in <tool_response>.
            out.append(f"<|im_start|>user\n<tool_response>\n{content}\n</tool_response><|im_end|>\n")
        else:
            out.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    out.append("<|im_start|>assistant\n")
    return "".join(out)


def completion(base_url: str, prompt: str, temperature: float, max_tokens: int) -> dict:
    payload = {
        "prompt": prompt,
        "temperature": temperature,
        "n_predict": max_tokens,
        "stop": ["<|im_end|>"],
        "cache_prompt": True,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/completion",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def parse_first_tool_call(text: str) -> dict | None:
    m = TOOL_CALL_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def fake_tool_result(call: dict, _prompt_text: str) -> str:
    """Return a short, plausible tool_result string.

    Varies by tool and by tool-arg keywords; designed to be informative enough
    that a turn-2 assistant could reasonably adapt rather than repeat itself.
    """
    name = call.get("name", "")
    args = call.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}

    if name == "Bash":
        cmd = (args.get("command") or "").strip()
        # plausible canned outputs by command keyword
        if "git log" in cmd or "git show" in cmd:
            return json.dumps({"stdout": "8621fdb docs: update HANDOFF + README + V2 plan (#43)\n3628be8 feat(fine-tune): build_v2_dataset.py — real prompts (#42)\nffa1769 fix(backfill): dedupe by position not content_hash (#41)", "stderr": ""})
        if "git diff" in cmd:
            return json.dumps({"stdout": "scripts/fine_tune/build_v2_dataset.py | 124 ++++++++++++\nscripts/fine_tune/validate_tool_calls.py | 18 +-\n2 files changed, 132 insertions(+), 10 deletions(-)", "stderr": ""})
        if "ls" in cmd and "gguf" in cmd.lower():
            return json.dumps({"stdout": "qwen2.5-3b-toolcalls-q4km.gguf  1.8G\nqwen2.5-3b-toolcalls-v2-q6k.gguf  2.4G", "stderr": ""})
        if "ls" in cmd:
            return json.dumps({"stdout": "build_v2_dataset.py\nvalidate_tool_calls.py\ntrain_lora.py\nconvert_to_gguf.py\nlib/", "stderr": ""})
        if "find" in cmd or "grep" in cmd:
            return json.dumps({"stdout": "scripts/fine_tune/build_v2_dataset.py:142:def build_system_prompt(\nscripts/fine_tune/validate_tool_calls.py:297:def _build_prompt_for_llama_cli(", "stderr": ""})
        if "wc" in cmd or "du" in cmd:
            return json.dumps({"stdout": "1.8G\tqwen2.5-3b-toolcalls-q4km.gguf\n2.4G\tqwen2.5-3b-toolcalls-v2-q6k.gguf", "stderr": ""})
        return json.dumps({"stdout": "ok", "stderr": ""})

    if name == "Read":
        fp = (args.get("file_path") or "").strip()
        if "validate_tool_calls" in fp:
            return "def _build_prompt_for_llama_cli(prompt, schemas):\n    tools_json = '\\n'.join(json.dumps(s) for s in schemas)\n    return ('<|im_start|>system\\n'\n            'You are Qwen, created by Alibaba Cloud. You are a helpful assistant with access to tools.\\n\\n'\n            '# Tools\\n\\n' ... )"
        if "build_v2_dataset" in fp:
            return "def build_training_row(session_id, messages, tool_schemas):\n    # v2: real prompts from claude jsonl, not synthetic\n    system_prompt = build_system_prompt(tool_schemas)\n    return {'messages': [...], 'tools': tool_schemas}"
        if "vague_prompts" in fp:
            return "find the fire-map codebase\nwhat does this repo do\nshow me the recent work"
        if "tool_schemas" in fp:
            return '{"Bash": {...}, "Read": {...}, "Write": {...}, "Grep": {...}, "Edit": {...}}'
        if "README" in fp or "HANDOFF" in fp:
            return "# agentMemory\\n\\nLocal-first memory + tool-call fine-tune. v2 fixes empty-args loop. ..."
        return f"<contents of {fp or 'file'}>"

    if name == "Grep":
        pat = (args.get("pattern") or "").strip()
        if "TODO" in pat or "todo" in pat:
            return "scripts/fine_tune/build_v2_dataset.py:67: # TODO: handle nested tool_use blocks\nscripts/fine_tune/lib/__init__.py:23: # TODO: consolidate utc helpers\nmcp_server/main.py:412: # TODO: redact PII from observations"
        if "empty_args" in pat or "AntiLoop" in pat or "anti_loop" in pat:
            return "scripts/fine_tune/validate_tool_calls.py:191:class AntiLoopDetector:\nscripts/fine_tune/validate_tool_calls.py:218: if args == {} or args is None:\nscripts/fine_tune/validate_tool_calls.py:475: 'empty_args_emissions_total':"
        if "leak" in pat.lower() or "memory" in pat.lower():
            return "mcp_server/main.py:301: # potential leak: handler refs kept in module-level dict\nmcp_server/main.py:415: gc.collect()"
        return f"<no matches for {pat}>"

    if name == "Edit":
        return json.dumps({"ok": True, "file": args.get("file_path", "")})
    if name == "Write":
        return json.dumps({"ok": True, "wrote": args.get("file_path", "")})

    return json.dumps({"stdout": "ok", "stderr": ""})


def detect_loop(calls_so_far: list[dict]) -> bool:
    """3 consecutive identical-or-empty-args calls => loop."""
    if len(calls_so_far) < 3:
        return False
    last3 = calls_so_far[-3:]
    # all empty-args
    if all((c.get("arguments") in ({}, None)) for c in last3):
        return True
    # all identical
    sigs = [json.dumps({"n": c.get("name"), "a": c.get("arguments") or {}}, sort_keys=True) for c in last3]
    return sigs[0] == sigs[1] == sigs[2]


def run_session(base_url: str, prompt_text: str, schemas: list[dict], temperature: float = 0.2, max_turns: int = 5, max_tokens: int = 512) -> dict:
    system_block = build_system_block(schemas)
    turns: list[dict] = [{"role": "user", "content": prompt_text}]
    transcript = []
    total_tokens = 0
    parsed_calls_seq: list[dict] = []
    outcome = "unknown"

    for turn_idx in range(max_turns):
        full_prompt = build_prompt(system_block, turns)
        t0 = time.time()
        try:
            resp = completion(base_url, full_prompt, temperature, max_tokens)
        except urllib.error.URLError as e:
            transcript.append({"turn": turn_idx + 1, "error": f"http error: {e}"})
            outcome = "error"
            break
        latency = time.time() - t0
        raw = resp.get("content", "")
        tokens_predicted = resp.get("tokens_predicted") or resp.get("timings", {}).get("predicted_n", 0)
        total_tokens += int(tokens_predicted or 0)

        call = parse_first_tool_call(raw)
        transcript.append({
            "turn": turn_idx + 1,
            "raw": raw,
            "tool_call": call,
            "latency_s": round(latency, 2),
            "tokens_predicted": tokens_predicted,
        })

        if call is None:
            outcome = "text_response"
            # Append the assistant turn for completeness; we're done
            turns.append({"role": "assistant", "content": raw})
            break

        parsed_calls_seq.append(call)
        if detect_loop(parsed_calls_seq):
            outcome = "looped"
            turns.append({"role": "assistant", "content": raw})
            break

        # Fake the tool result and feed it back
        fake = fake_tool_result(call, prompt_text)
        turns.append({"role": "assistant", "content": raw})
        turns.append({"role": "tool", "content": fake})
        transcript[-1]["fake_tool_result"] = fake

    else:
        # Loop ran to max_turns without a text_response or loop
        outcome = "max_turns"

    return {
        "prompt": prompt_text,
        "outcome": outcome,
        "turns": transcript,
        "total_tokens": total_tokens,
        "parsed_calls": parsed_calls_seq,
    }


# ---- Analysis helpers --------------------------------------------------------

def analyze_session(s: dict) -> dict:
    turn1 = s["turns"][0] if s["turns"] else {}
    turn1_call = turn1.get("tool_call")

    emitted_turn1 = turn1_call is not None
    if emitted_turn1:
        args = turn1_call.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        # populated = has at least one required field with a non-empty value
        populated = len(args) > 0 and any(v not in (None, "", {}, []) for v in args.values())
        args_quality = "yes" if populated else ("partial" if args else "no")
    else:
        args_quality = "n/a"

    looped = s["outcome"] == "looped"

    # multi-turn adapted: any turn after turn 1 has a different (name, args) than turn 1
    adapted = False
    if len(s["turns"]) > 1 and emitted_turn1:
        for tr in s["turns"][1:]:
            tc = tr.get("tool_call")
            if tc is None:
                # text response after tool result counts as adapted (synthesized answer)
                adapted = True
                break
            if tc.get("name") != turn1_call.get("name") or tc.get("arguments") != turn1_call.get("arguments"):
                adapted = True
                break

    # Outcome classification
    if looped:
        final = "looped"
    elif s["outcome"] == "text_response" and not emitted_turn1:
        final = "text_only_fallback"
    elif s["outcome"] == "text_response":
        # text response after at least one tool_call → useful answer
        final = "useful_answer"
    elif s["outcome"] == "max_turns":
        final = "gave_up"
    elif s["outcome"] == "error":
        final = "error"
    else:
        final = s["outcome"]

    return {
        "emitted_tool_call_turn1": "yes" if emitted_turn1 else "no",
        "args_populated_turn1": args_quality,
        "looped": "yes" if looped else "no",
        "multi_turn_adapted": "yes" if adapted else "no",
        "final_outcome": final,
        "total_tokens": s["total_tokens"],
        "n_turns": len(s["turns"]),
    }


# ---- Entrypoint --------------------------------------------------------------

PROMPTS = [
    "Help me find where the validator system prompt is built. Show me the actual code.",
    "What changed in the fine_tune scripts in the last week?",
    "Is there a test that proves the empty-args loop is fixed? Show me.",
    "I think there might be a memory leak in the MCP server. Investigate.",
    "Generate a summary of all GGUF files in this repo and their sizes.",
    "Find every TODO comment in the Python code and group them by file.",
    "What's the difference between v1 and v2 training data?",
    "Walk me through how a training row goes from raw .jsonl to the dataset.",
    "Show me the most recent commit and what it changed.",
    "How do I run the validator? Give me the exact command.",
]


def main():
    if len(sys.argv) < 3:
        print("usage: harness.py <base_url> <out.json>")
        sys.exit(2)
    base_url, out_path = sys.argv[1], sys.argv[2]
    schemas = load_schemas()
    results = []
    for i, p in enumerate(PROMPTS, 1):
        print(f"[{base_url}] prompt {i}/{len(PROMPTS)}: {p[:60]}", flush=True)
        sess = run_session(base_url, p, schemas)
        sess["analysis"] = analyze_session(sess)
        results.append(sess)
        print(f"   -> outcome={sess['analysis']['final_outcome']} turns={sess['analysis']['n_turns']} tokens={sess['total_tokens']}", flush=True)
    Path(out_path).write_text(json.dumps(results, indent=2))
    print(f"wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
