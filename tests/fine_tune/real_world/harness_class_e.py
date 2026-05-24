#!/usr/bin/env python3
"""Class E — project-specific recall.

For each prompt in tests/fine_tune/fixtures/project_recall_prompts.txt, send a
single-turn chat-completions request and classify the response by heuristics:

  PASS     references specific files / paths / concepts from this codebase
  PARTIAL  references the project name but no specific files/concepts
  FAIL     generic answer with no project-specific content

The schema's eval_class enum does not include "E"; we set eval_class="custom"
and tag aggregate_stats with `eval_class_label="E"` so the report still renders.

Usage:
    python harness_class_e.py <base_url> <model> <model_id> <quant> <out.json> \
        [--prompts FILE] [--report-id ID]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path("/Users/mz/_CODING/agentMemory")
PROMPTS_FILE = REPO / "tests" / "fine_tune" / "fixtures" / "project_recall_prompts.txt"

# Concrete project-specific tokens. Any one hit in the response => PASS.
# These are deliberately drawn from filenames, module names, and concepts that
# only appear in this codebase or the user's known related projects.
SPECIFIC_TOKENS = [
    "mem_sessions", "mem_projects", "mem_observations",
    "validate_tool_calls", "build_v2_dataset", "harness_chat", "harness_class",
    "defaultLayers", "fire-map.wfca.com", "agent_memory_token",
    "AntiLoopDetector", "empty_args", "anti-loop", "anti_loop",
    "qwen2.5-3b-toolcalls", "qwen25_tools/v2", "tool_schemas.json",
    "valid.chat.jsonl", "train.chat.jsonl", "valid.tiny.jsonl",
    "HANDOFF.md", "CLAUDE.md", "AGENTS.md", "V3_PLAN", "v2-real-world-test",
    "Daily Dispatch", "daily-dispatch", "dispatch_briefing",
    "Anvil", "anvil_run", "anvil_task_create", ".anvil",
    "fire-map", "wfca", "psde-os", "psde-mz-test",
    "agent-memory", "agentMemory", "mcp_server",
    "session-start", "PreToolUse", "PostToolUse",
    "Riverpod", "GoRouter",  # only for project-flutter-dev mentions
    "tool_calls", "Hermes", "ChatML",
    "qwen25-toolcalls", "Q4_K_M", "Q6_K", "GGUF",
    "ETL", "GeoServer", "Amplify", "EBS",
]

# Project-name tokens. Hit but no specific token => PARTIAL.
PROJECT_NAME_TOKENS = [
    "agent-memory", "agentMemory", "agent_memory",
    "Daily Dispatch", "daily-dispatch",
    "fire-map", "wfca", "fire map", "Fire Map",
    "Anvil", "anvil",
]


def classify(response: str) -> tuple[str, list[str]]:
    """Return (verdict, hits)."""
    text = response or ""
    low = text.lower()
    specific_hits = []
    for tok in SPECIFIC_TOKENS:
        if tok.lower() in low:
            specific_hits.append(tok)
    if specific_hits:
        return "PASS", specific_hits
    project_hits = []
    for tok in PROJECT_NAME_TOKENS:
        if tok.lower() in low:
            project_hits.append(tok)
    if project_hits:
        return "PARTIAL", project_hits
    return "FAIL", []


def chat_completion(base_url: str, model: str, user_text: str, temperature: float,
                    max_tokens: int) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."},
            {"role": "user", "content": user_text},
        ],
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("model")
    ap.add_argument("model_id")
    ap.add_argument("quant")
    ap.add_argument("out_path")
    ap.add_argument("--prompts", default=str(PROMPTS_FILE))
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--report-id", default=None)
    ap.add_argument("--model-path", default=None)
    args = ap.parse_args()

    prompts = [ln.strip() for ln in Path(args.prompts).read_text().splitlines() if ln.strip()]
    print(f"loaded {len(prompts)} prompts from {args.prompts}", flush=True)

    counts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "ERROR": 0}
    prompts_out = []

    for i, p in enumerate(prompts, 1):
        t0 = time.time()
        text = ""
        tokens = 0
        verdict = "ERROR"
        hits: list[str] = []
        details: dict[str, Any] = {}
        try:
            resp = chat_completion(args.base_url, args.model, p, args.temperature, args.max_tokens)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            usage = resp.get("usage") or {}
            tokens = int(usage.get("completion_tokens") or 0)
            verdict, hits = classify(text)
            details = {"hits": hits[:10], "preview": text[:240]}
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
        print(f"  [{i}/{len(prompts)}] {verdict} ({latency}s) hits={hits[:3]}", flush=True)

        notes = json.dumps(details, ensure_ascii=False)[:500]
        prompts_out.append({
            "id": i,
            "text": p,
            "expected_intent": "project_specific_recall",
            "outcome": _verdict_to_outcome(verdict),
            "n_turns": 1,
            "total_tokens": tokens,
            "notes": notes,
            "regressions": [],
        })

    n = sum(counts.values())
    pass_rate = counts["PASS"] / n if n else 0.0
    fail_rate = counts["FAIL"] / n if n else 0.0

    report = {
        "report_id": args.report_id or f"class-e-{args.model_id}-{date.today().isoformat()}",
        "run_date": date.today().isoformat(),
        "model": {
            "id": args.model_id,
            "path": args.model_path or "",
            "quant": args.quant,
            "params": "3B",
        },
        "harness": {
            "name": "harness_class_e.py",
            "version": "phase-0.5-baseline-20260515",
            "endpoint": "/v1/chat/completions",
            "server": f"llama-server --jinja -c 8192 ({args.base_url})",
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "max_turns": 1,
        },
        "eval_class": "custom",
        "gates": [
            {
                "name": "PASS rate",
                "threshold": ">= 50",
                "actual": round(pass_rate * 100, 1),
                "units": "%",
                "pass": pass_rate >= 0.50,
                "notes": f"{counts['PASS']}/{n} responses mention concrete project files/concepts",
            },
            {
                "name": "FAIL rate",
                "threshold": "<= 30",
                "actual": round(fail_rate * 100, 1),
                "units": "%",
                "pass": fail_rate <= 0.30,
                "notes": f"{counts['FAIL']}/{n} responses are generic with no project-specific signal",
            },
        ],
        "prompts": prompts_out,
        "aggregate_stats": {
            "PASS": f"{counts['PASS']}/{n}",
            "PARTIAL": f"{counts['PARTIAL']}/{n}",
            "FAIL": f"{counts['FAIL']}/{n}",
            "ERROR": f"{counts['ERROR']}/{n}",
            "pass_rate_pct": round(pass_rate * 100, 1),
            "eval_class_label": "E",
        },
        "notable_findings": (
            "Class E probes whether the fine-tune absorbed project-specific recall (file names, "
            "module names, concept names from agentMemory / Daily Dispatch / Fire Map / Anvil). "
            "PASS requires at least one concrete-token hit from the SPECIFIC_TOKENS allow-list; "
            "PARTIAL requires only a project-name hit; FAIL means generic text with no project "
            "signal at all. The schema enum does not include 'E', so eval_class is set to "
            "'custom' (aggregate_stats.eval_class_label='E'); extending the schema enum is "
            "tracked as an open follow-up."
        ),
        "verdict": {
            "pass": pass_rate >= 0.50,
            "headline": (
                f"Class E recall: {counts['PASS']}/{n} PASS, "
                f"{counts['PARTIAL']}/{n} PARTIAL, "
                f"{counts['FAIL']}/{n} FAIL."
            ),
            "recommendation": (
                "Use these baselines to measure v3's project knowledge absorption. If v1/v2 are "
                "both mostly FAIL, project-specific recall was never a training signal — that's "
                "fine; for v3, add a dedicated project-recall split to the dataset (mem_observations + "
                "filename-bearing prompts) and re-measure."
            ),
        },
        "artifacts": [
            {"name": "raw transcripts", "path": args.out_path},
        ],
    }

    Path(args.out_path).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out_path}", flush=True)
    print(f"counts: {counts}  pass_rate={pass_rate:.1%}", flush=True)
    return 0


def _verdict_to_outcome(v: str) -> str:
    return {
        "PASS": "useful_answer",
        "PARTIAL": "text_answer",
        "FAIL": "text_only_fallback",
        "ERROR": "error",
    }.get(v, "off_topic")


if __name__ == "__main__":
    sys.exit(main())
