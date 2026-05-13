#!/usr/bin/env python3
"""Validate that a fine-tuned model emits parseable Hermes-style tool calls.

Drives one of three backends:
  - llama-cli       (local GGUF, fast smoke test)
  - openai          (LM Studio / Ollama OpenAI-compatible server on localhost)
  - hf              (transformers/MPS inference for a merged HF model)

Reusable across any Qwen2.5 / Qwen3 / Hermes-style tool-call model.

Pass criteria are configurable:
    --min-parse-rate 0.03   (Phase 4 tiny: ≥ 1/30)
    --min-parse-rate 0.8    (Phase 5 full: ≥ 24/30)

Outputs a JSON report at <report-dir>/validate_<UTC>.json with per-prompt
generations, parse results, and schema-validation outcomes.

Usage:
    python scripts/fine_tune/validate_tool_calls.py \\
        --backend llama-cli \\
        --gguf models/gguf/qwen2.5-3b-toolcalls-q4km.gguf \\
        --min-parse-rate 0.03

    python scripts/fine_tune/validate_tool_calls.py \\
        --backend openai \\
        --base-url http://localhost:1234/v1 \\
        --model qwen25-toolcalls \\
        --min-parse-rate 0.8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import REPO_ROOT, utc_stamp, write_json  # noqa: E402


# ---- Test fixtures (canonical, version-pinned) -----------------------------

# In-distribution tools — loaded from the training dataset's schema registry.
# These are the tools the model was actually trained on, wrapped in OpenAI-style
# function-tool envelopes for apply_chat_template / Hermes-format inference.
# Keep the tool list small — the model attends over the whole tools block on
# every generation. ~5 tools keeps the prompt under ~2.5KB and generation under
# ~3s on M-series MPS. The 5 below are the most common in the training set.
DEFAULT_TRAINED_TOOLS = ["Bash", "Read", "Write", "Grep", "Edit"]


def _load_trained_tool_schemas(only: list[str] | None = None) -> list[dict]:
    schemas_file = REPO_ROOT / "data" / "processed" / "qwen25_tools" / "v1" / "tool_schemas.json"
    if not schemas_file.exists():
        return _FALLBACK_TOOL_SCHEMAS
    with schemas_file.open() as f:
        registry = json.load(f)
    keep = set(only or DEFAULT_TRAINED_TOOLS)
    out = []
    for name, sch in registry.items():
        if name not in keep:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"Tool '{name}' (schema inferred from training data).",
                "parameters": {
                    "type": "object",
                    "properties": sch.get("properties", {}),
                    "required": sch.get("required", []),
                },
            },
        })
    return out


# Used if the training dataset isn't available (e.g. testing the validator in CI)
_FALLBACK_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "get_weather", "description": "Get weather.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, "required": ["city"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_bash", "description": "Run bash command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"}}, "required": ["command"]}}},
]

TOOL_SCHEMAS = _load_trained_tool_schemas() if (REPO_ROOT / "data" / "processed" / "qwen25_tools" / "v1" / "tool_schemas.json").exists() else _FALLBACK_TOOL_SCHEMAS

# Two prompt suites — both important:
#   IN_DISTRIBUTION: mirrors training prompts ("Call tool `X` with appropriate arguments.")
#       — tests memorization, useful for tiny-pipeline integrity check.
#   NATURAL: realistic agentic asks — tests generalization, what real LM Studio
#       users will type.
IN_DISTRIBUTION_PROMPTS = [
    "Call tool `Bash` with appropriate arguments.",
    "Call tool `Read` with appropriate arguments.",
    "Call tool `Write` with appropriate arguments.",
    "Call tool `Grep` with appropriate arguments.",
    "Call tool `Edit` with appropriate arguments.",
]

NATURAL_PROMPTS = [
    "Read the file at /etc/hostname for me.",
    "Run `ls -la /tmp` and tell me what's there.",
    "Search the codebase for any function named `parse_tool_call`.",
    "Find all .py files in the src directory.",
    "Write a hello world script to /tmp/hi.py.",
]

CANONICAL_PROMPTS = IN_DISTRIBUTION_PROMPTS + NATURAL_PROMPTS

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# ---- Parsing ---------------------------------------------------------------

@dataclass
class ParseResult:
    parsed: bool
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    schema_valid: bool = False
    error: str | None = None


_CONSECUTIVE_THRESHOLD = 3


def _normalize_call(call: dict[str, Any]) -> str:
    name = call.get("name", "")
    args = call.get("arguments", {})
    args_canon = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{name}|{args_canon}".encode()).hexdigest()


@dataclass
class AntiLoopDecision:
    suppress: bool
    reason: str | None = None
    streak: int = 0


class AntiLoopDetector:
    """Detects the empty-args infinite-loop pattern observed in v1.

    Tracks the last N normalized tool calls in a conversation. When the same
    (name, arguments) pair appears `threshold` times in a row, the next
    identical call is flagged for suppression — the caller should drop the
    tool_call block and emit a text response instead.

    Stateful per conversation. Instantiate one detector per session.

    The production hook point is in `mcp_server.py` and the Claude hooks;
    this class lives in the validator so the same logic powers both the
    runtime guard and the offline eval suite.
    """

    def __init__(self, threshold: int = _CONSECUTIVE_THRESHOLD, model_version: str | None = None):
        if threshold < 2:
            raise ValueError("anti-loop threshold must be >= 2")
        self.threshold = threshold
        self.model_version = model_version
        self._history: deque[str] = deque(maxlen=threshold)
        self.suppressions = 0
        self.empty_args_emissions = 0

    def observe(self, call: dict[str, Any]) -> AntiLoopDecision:
        """Record a tool call; return whether the next emission would loop."""
        args = call.get("arguments")
        if args == {} or args is None:
            self.empty_args_emissions += 1

        sig = _normalize_call(call)
        self._history.append(sig)

        if len(self._history) < self.threshold:
            return AntiLoopDecision(suppress=False, streak=self._history.count(sig))

        if all(h == sig for h in self._history):
            self.suppressions += 1
            log.warning(
                "anti_loop_suppress model=%s streak=%s name=%s",
                self.model_version, self.threshold, call.get("name"),
            )
            return AntiLoopDecision(
                suppress=True,
                reason=f"{self.threshold} consecutive identical calls",
                streak=self.threshold,
            )
        return AntiLoopDecision(suppress=False, streak=self._history.count(sig))

    def filter_sequence(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convenience: filter a known sequence, returning calls that survive."""
        out: list[dict[str, Any]] = []
        for c in calls:
            decision = self.observe(c)
            if not decision.suppress:
                out.append(c)
        return out


def parse_tool_calls(text: str, schemas: list[dict]) -> ParseResult:
    """Extract and validate <tool_call> blocks from generated text."""
    matches = TOOL_CALL_RE.findall(text)
    if not matches:
        return ParseResult(parsed=False, error="no <tool_call> tags found")
    tool_calls = []
    for raw in matches:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return ParseResult(parsed=False, error=f"json: {e}")
        if "name" not in obj or "arguments" not in obj:
            return ParseResult(parsed=False, error=f"missing name/arguments in {obj!r}")
        tool_calls.append(obj)

    # Schema validation (jsonschema is optional — degrade gracefully)
    schema_valid = True
    schema_err: str | None = None
    try:
        from jsonschema import Draft7Validator, ValidationError  # noqa: PLC0415

        registry = {s["function"]["name"]: s["function"]["parameters"] for s in schemas}
        for tc in tool_calls:
            params_schema = registry.get(tc["name"])
            if params_schema is None:
                schema_valid = False
                schema_err = f"unknown tool name: {tc['name']}"
                break
            try:
                Draft7Validator(params_schema).validate(tc["arguments"])
            except ValidationError as e:
                schema_valid = False
                schema_err = f"args invalid for {tc['name']}: {e.message}"
                break
    except ImportError:
        schema_err = "jsonschema not installed; skipping schema validation"

    return ParseResult(
        parsed=True,
        tool_calls=tool_calls,
        schema_valid=schema_valid,
        error=schema_err,
    )


# ---- Backends --------------------------------------------------------------

def _build_prompt_for_llama_cli(prompt: str, schemas: list[dict]) -> str:
    """Build the full ChatML string Qwen 2.5 expects when tools are in scope."""
    tools_json = "\n".join(json.dumps(s) for s in schemas)
    return (
        "<|im_start|>system\n"
        "You are Qwen, created by Alibaba Cloud. You are a helpful assistant.\n\n"
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        f"<tools>\n{tools_json}\n</tools>\n\n"
        "For each function call, return a json object with function name and arguments within "
        "<tool_call></tool_call> XML tags:\n"
        '<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>'
        "<|im_end|>\n"
        f"<|im_start|>user\n{prompt}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def gen_llama_cli(gguf: Path, prompt: str, schemas: list[dict], temperature: float, max_tokens: int) -> str:
    """Drive models/llama.cpp/build/bin/llama-cli."""
    cli = REPO_ROOT / "models" / "llama.cpp" / "build" / "bin" / "llama-cli"
    if not cli.exists():
        raise FileNotFoundError(f"llama-cli not found at {cli}")
    full_prompt = _build_prompt_for_llama_cli(prompt, schemas)
    cmd = [
        str(cli),
        "-m", str(gguf),
        "-p", full_prompt,
        "-n", str(max_tokens),
        "--temp", str(temperature),
        "-st",  # single-turn — exit after one generation
        "--no-warmup",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    # llama-cli echoes the prompt; strip it
    out = result.stdout
    if full_prompt in out:
        out = out.split(full_prompt, 1)[1]
    return out


# Lazy globals for the HF backend (load model exactly once across all trials)
_HF_TOK = None
_HF_MODEL = None
_HF_DEVICE = None


def gen_hf(hf_model_dir: str, prompt: str, schemas: list[dict], temperature: float, max_tokens: int) -> str:
    """Drive a merged HF model directly via transformers. Use to validate
    BEFORE GGUF conversion — catches merge-time bugs without llama.cpp.
    """
    global _HF_TOK, _HF_MODEL, _HF_DEVICE
    if _HF_MODEL is None:
        import torch  # noqa: PLC0415
        from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

        _HF_DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.bfloat16 if _HF_DEVICE == "mps" else torch.float32
        _HF_TOK = AutoTokenizer.from_pretrained(hf_model_dir, local_files_only=True, trust_remote_code=False)
        _HF_MODEL = AutoModelForCausalLM.from_pretrained(
            hf_model_dir, local_files_only=True, trust_remote_code=False, torch_dtype=dtype,
        ).to(_HF_DEVICE)
        _HF_MODEL.eval()

    import torch  # noqa: PLC0415
    messages = [{"role": "user", "content": prompt}]
    text = _HF_TOK.apply_chat_template(messages, tools=schemas, tokenize=False, add_generation_prompt=True)
    inputs = _HF_TOK(text, return_tensors="pt").to(_HF_DEVICE)
    with torch.no_grad():
        out = _HF_MODEL.generate(
            **inputs,
            max_new_tokens=max_tokens,
            do_sample=(temperature > 0),
            temperature=max(temperature, 1e-5),
            pad_token_id=_HF_TOK.eos_token_id,
        )
    return _HF_TOK.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=False)


def gen_openai(base_url: str, model: str, prompt: str, schemas: list[dict], temperature: float, max_tokens: int) -> dict:
    """Drive an OpenAI-compatible /v1/chat/completions endpoint (LM Studio, Ollama, vLLM)."""
    import urllib.request  # noqa: PLC0415

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": schemas,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


# ---- Runner ----------------------------------------------------------------

@dataclass
class TrialResult:
    prompt: str
    temperature: float
    raw_text: str
    parse: ParseResult
    structured_tool_calls: list[dict] | None = None  # from OpenAI API response


def run(args) -> int:
    schemas = TOOL_SCHEMAS
    temps = [float(t) for t in args.temperatures.split(",")]
    trials: list[TrialResult] = []

    in_dist_set = set(IN_DISTRIBUTION_PROMPTS)
    natural_set = set(NATURAL_PROMPTS)

    for prompt in CANONICAL_PROMPTS:
        suite = "in_distribution" if prompt in in_dist_set else "natural"
        _ = suite  # tagged below in trial record
        for temp in temps:
            print(f"[{args.backend}] T={temp:.2f}  {prompt[:60]}")
            structured: list[dict] | None = None
            try:
                if args.backend == "llama-cli":
                    raw = gen_llama_cli(Path(args.gguf), prompt, schemas, temp, args.max_tokens)
                elif args.backend == "hf":
                    raw = gen_hf(args.hf_model_dir, prompt, schemas, temp, args.max_tokens)
                elif args.backend == "openai":
                    resp = gen_openai(args.base_url, args.model, prompt, schemas, temp, args.max_tokens)
                    msg = resp["choices"][0]["message"]
                    raw = msg.get("content") or ""
                    structured = msg.get("tool_calls")
                    # If server parsed Hermes natively, synthesize <tool_call> blocks for our parser
                    if structured:
                        for tc in structured:
                            fn = tc.get("function", {})
                            raw += (
                                f"\n<tool_call>\n"
                                + json.dumps({"name": fn.get("name"), "arguments": json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else fn.get("arguments", {})})
                                + "\n</tool_call>"
                            )
                else:
                    raise ValueError(f"unknown backend: {args.backend}")
            except Exception as e:
                trials.append(TrialResult(prompt=prompt, temperature=temp, raw_text="", parse=ParseResult(parsed=False, error=f"backend error: {e}")))
                continue

            parse = parse_tool_calls(raw, schemas)
            trials.append(TrialResult(prompt=prompt, temperature=temp, raw_text=raw, parse=parse, structured_tool_calls=structured))

    n = len(trials)
    n_parsed = sum(1 for t in trials if t.parse.parsed)
    n_schema_valid = sum(1 for t in trials if t.parse.parsed and t.parse.schema_valid)
    n_native = sum(1 for t in trials if t.structured_tool_calls)
    parse_rate = n_parsed / n if n else 0.0
    valid_rate = n_schema_valid / n if n else 0.0

    anti_loop_report: dict[str, Any] | None = None
    if getattr(args, "anti_loop", False):
        detector = AntiLoopDetector(model_version=args.model_version)
        for t in trials:
            for call in t.parse.tool_calls:
                detector.observe(call)
        anti_loop_report = {
            "enabled": True,
            "threshold": detector.threshold,
            "suppressions": detector.suppressions,
            "empty_args_emissions_total": detector.empty_args_emissions,
            "model_version": detector.model_version,
        }

    # Per-suite breakdown
    def _suite_of(p: str) -> str:
        return "in_distribution" if p in in_dist_set else ("natural" if p in natural_set else "other")
    by_suite: dict[str, dict[str, int]] = {}
    for t in trials:
        s = _suite_of(t.prompt)
        d = by_suite.setdefault(s, {"trials": 0, "parsed": 0, "schema_valid": 0})
        d["trials"] += 1
        if t.parse.parsed:
            d["parsed"] += 1
            if t.parse.schema_valid:
                d["schema_valid"] += 1

    report = {
        "backend": args.backend,
        "model": getattr(args, "model", None) or getattr(args, "gguf", None),
        "trials": n,
        "parsed": n_parsed,
        "schema_valid": n_schema_valid,
        "native_tool_calls": n_native,
        "parse_rate": parse_rate,
        "valid_rate": valid_rate,
        "by_suite": by_suite,
        "min_required_parse_rate": args.min_parse_rate,
        "passed": parse_rate >= args.min_parse_rate,
        "anti_loop": anti_loop_report,
        "utc": utc_stamp(),
        "trials_detail": [
            {
                "prompt": t.prompt,
                "temperature": t.temperature,
                "raw_text": t.raw_text[:2000],
                "parsed": t.parse.parsed,
                "schema_valid": t.parse.schema_valid,
                "error": t.parse.error,
                "tool_calls": t.parse.tool_calls,
                "native_tool_calls": t.structured_tool_calls,
            }
            for t in trials
        ],
    }

    report_dir = Path(args.report_dir) if args.report_dir else REPO_ROOT / "logs" / "m-ft-1"
    report_path = report_dir / f"validate_{args.backend}_{utc_stamp()}.json"
    write_json(report_path, report)

    print()
    print(f"REPORT: {report_path}")
    print(f"  parsed:        {n_parsed}/{n}  ({parse_rate:.1%})")
    print(f"  schema-valid:  {n_schema_valid}/{n}  ({valid_rate:.1%})")
    print(f"  native:        {n_native}/{n}")
    for suite_name, d in sorted(by_suite.items()):
        rate = d["parsed"] / d["trials"] if d["trials"] else 0.0
        print(f"  [{suite_name:16s}] parsed {d['parsed']}/{d['trials']} ({rate:.1%}) schema-valid {d['schema_valid']}/{d['trials']}")
    print(f"  required:      ≥{args.min_parse_rate:.1%}")
    if anti_loop_report:
        print(
            f"  anti-loop:     suppressions={anti_loop_report['suppressions']} "
            f"empty_args={anti_loop_report['empty_args_emissions_total']} "
            f"model={anti_loop_report['model_version']}"
        )
    print(f"  result:        {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=["llama-cli", "openai", "hf"], required=True)
    p.add_argument("--gguf", help="Path to GGUF (llama-cli backend)")
    p.add_argument("--hf-model-dir", help="Path to merged HF model dir (hf backend)")
    p.add_argument("--base-url", default="http://localhost:1234/v1", help="OpenAI-compatible base URL")
    p.add_argument("--model", default="qwen25-toolcalls", help="Model name for openai backend")
    p.add_argument("--temperatures", default="0.0,0.2,0.7")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--min-parse-rate", type=float, default=0.03, help="Minimum parse rate to pass")
    p.add_argument("--report-dir", default=None)
    p.add_argument(
        "--anti-loop", action="store_true",
        help="Run anti-loop guard across the trial sequence and report suppressions.",
    )
    p.add_argument(
        "--model-version", default=None,
        help="Tag attached to anti-loop suppression logs and the empty_args counter (e.g. v1, v2).",
    )
    args = p.parse_args()

    if args.backend == "llama-cli" and not args.gguf:
        p.error("--gguf required for llama-cli backend")
    if args.backend == "hf" and not args.hf_model_dir:
        p.error("--hf-model-dir required for hf backend")

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
