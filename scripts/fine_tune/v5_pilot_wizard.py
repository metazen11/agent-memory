#!/usr/bin/env python3
"""v5 pilot config wizard.

Walks the user through the design choices for the v5 pilot dataset + run:
  - source window (how many recent rows to sample from)
  - project exclusion list (noise filtering)
  - continuation-prompt filter (drop near-empty user turns)
  - strip injected reminder blocks from prompts
  - dedup strategy
  - target row count
  - base model
  - smoke vs full run

Writes a single yaml file the builder + launcher both read, so there's one
authoritative config for the pilot run. No silent defaults — every choice
is shown with its tradeoff.

Usage:
    python3 scripts/fine_tune/v5_pilot_wizard.py
    python3 scripts/fine_tune/v5_pilot_wizard.py --out configs/v5_pilot.yaml
    python3 scripts/fine_tune/v5_pilot_wizard.py --defaults   # accept all defaults, no prompt
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


# --- Default config ----------------------------------------------------------

@dataclass
class V5PilotConfig:
    # Source query window
    source_window_recent_rows: int = 15000  # F1: pull deeper, filter to target
    target_clean_rows: int = 5000

    # Project exclusions (noise/synthetic projects)
    exclude_projects: list[str] = field(default_factory=lambda: [
        "test",
        "my-repo",
        "my-project",            # pytest fixture name
        "DailyDispatch.local",   # bonjour-style host noise
        "ws_b",
        "lib",
        "a", "b", "c",           # single-letter pytest src dirs
    ])
    # Glob patterns matched against project_id (case-insensitive)
    exclude_project_patterns: list[str] = field(default_factory=lambda: [
        "agent-a*",
        "ab-anvil-workspace*",
    ])
    # Path-sanity gate: drop rows whose cwd starts with any of these
    # (pytest tmpdirs, system temp, etc — pure test pollution).
    exclude_cwd_prefixes: list[str] = field(default_factory=lambda: [
        "/tmp/",
        "/private/var/folders/",
        "/var/folders/",
        "/private/tmp/",
    ])
    # If true: drop rows whose cwd does NOT normalize to <TRUSTED_ROOT>/...
    # (i.e., cwd is outside the known workspace prefixes).
    require_cwd_in_workspace: bool = True

    # Prompt quality filters
    min_prompt_len_chars: int = 20
    drop_continuation_prompts: bool = True
    # Regex applied to lowercased stripped prompt; if matches AND len < min, drop
    continuation_prompt_pattern: str = (
        r"^(yes|ok|sure|go|yeah|y|n|no|do it|please|"
        r"and|also|ok let's|let's|next|continue|more)\b"
    )
    strip_injected_blocks: bool = True  # remove <agent-memory>...</agent-memory> etc

    # Row-level required fields
    require_tool_response: bool = True   # tool_response_preview IS NOT NULL
    require_no_tool_error: bool = True   # tool_error IS NULL
    require_prompt_text: bool = True

    # Dedup
    dedup_key: str = "prompt_text+tool_input+cwd"  # tuple-key dedup, keep first

    # Base model + run mode
    base_model: str = "Qwen/Qwen2.5-3B-Instruct"
    run_mode: str = "smoke"   # "smoke" (50 rows, ~5min) or "full"

    # Output
    output_jsonl: str = "datasets/v5_pilot/train.jsonl"
    output_audit_md: str = "datasets/v5_pilot/AUDIT.md"


# --- Wizard questions --------------------------------------------------------

@dataclass
class Question:
    field_name: str
    prompt: str
    why: str
    default_repr: str         # how to show the default
    parser: Callable[[str], object]   # str -> typed value
    field_type: str = "str"   # "str" | "int" | "bool" | "list"


def _yn(s: str) -> bool:
    s = s.strip().lower()
    if s in {"y", "yes", "true", "1"}:
        return True
    if s in {"n", "no", "false", "0"}:
        return False
    raise ValueError(f"expected y/n, got {s!r}")


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


QUESTIONS: list[Question] = [
    Question(
        "source_window_recent_rows",
        "How many recent tool_call rows to pull from DB as source window?",
        "Deeper window = more rows survive the noise/quality filters. "
        "10000 → ~3-4k clean, 15000 → ~5k clean, 25000 → ~8k clean.",
        "15000",
        int, "int",
    ),
    Question(
        "target_clean_rows",
        "Target clean row count for the pilot?",
        "Smaller pilot = faster iteration but weaker signal. 5000 is the sweet "
        "spot for Qwen2.5-3B on MPS (~3-6h training).",
        "5000",
        int, "int",
    ),
    Question(
        "exclude_projects",
        "Projects to EXCLUDE (comma-separated, exact match)?",
        "These are noise/test projects that pollute training. Defaults drop "
        "obvious junk; keep psde_mz_test (your staging) and mz-personal-archived.",
        "test, my-repo, ws_b, lib",
        _csv, "list",
    ),
    Question(
        "exclude_project_patterns",
        "Project glob patterns to EXCLUDE (comma-separated)?",
        "Drops ephemeral synthetic project IDs like agent-a17xy and "
        "ab-anvil-workspace-* experiment workspaces.",
        "agent-a*, ab-anvil-workspace*",
        _csv, "list",
    ),
    Question(
        "min_prompt_len_chars",
        "Drop user prompts shorter than N characters?",
        "Short prompts like 'yes' or 'ok' are continuation turns missing prior "
        "context — they teach the model to act on near-empty input. 20 is "
        "moderate; 40 is strict; 0 disables length filter.",
        "20",
        int, "int",
    ),
    Question(
        "drop_continuation_prompts",
        "Drop continuation-style prompts (yes/ok/do it/and...) below min length? (y/n)",
        "Even short prompts can be valid ('list files'). The continuation filter "
        "only drops if it ALSO matches a continuation pattern.",
        "y",
        _yn, "bool",
    ),
    Question(
        "strip_injected_blocks",
        "Strip <agent-memory>...</agent-memory> blocks from captured user prompts? (y/n)",
        "These are runtime-injected reminders; if any leaked into prompt_text "
        "they should be removed so we don't teach the model to expect them.",
        "y",
        _yn, "bool",
    ),
    Question(
        "base_model",
        "Base model HF id?",
        "Qwen2.5-3B-Instruct = pilot (faster, smaller, validates pipeline). "
        "Qwen3-4B-Instruct = production target. Pilot first.",
        "Qwen/Qwen2.5-3B-Instruct",
        str, "str",
    ),
    Question(
        "run_mode",
        "Run mode: 'smoke' (50 rows, ~5min) or 'full' (target rows, ~3-6h)?",
        "Always smoke first to verify prompt assembles + trainer accepts the "
        "schema. Then full.",
        "smoke",
        str, "str",
    ),
]


# --- Driver ------------------------------------------------------------------

def ask_all(cfg: V5PilotConfig, accept_defaults: bool = False) -> V5PilotConfig:
    print("=" * 70)
    print("v5 PILOT CONFIG WIZARD")
    print("=" * 70)
    print()
    print("Answer or press ENTER to accept default. Ctrl-C to abort.")
    print()

    for q in QUESTIONS:
        print("-" * 70)
        print(f"Q: {q.prompt}")
        print(f"   why: {q.why}")
        if accept_defaults:
            print(f"   [default accepted] {q.default_repr}")
            continue
        raw = input(f"   [default: {q.default_repr}] > ").strip()
        if not raw:
            continue
        try:
            value = q.parser(raw)
        except Exception as e:
            print(f"   ERROR parsing {raw!r}: {e}. Keeping default.")
            continue
        setattr(cfg, q.field_name, value)

    print()
    print("=" * 70)
    print("FINAL CONFIG")
    print("=" * 70)
    for k, v in asdict(cfg).items():
        print(f"  {k}: {v}")
    print()
    return cfg


def write_yaml(cfg: V5PilotConfig, out_path: Path) -> None:
    """Write config as yaml (without pulling pyyaml as a dep — hand-format)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# v5 pilot config — generated by v5_pilot_wizard.py", ""]
    for k, v in asdict(cfg).items():
        if isinstance(v, bool):
            lines.append(f"{k}: {str(v).lower()}")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        elif isinstance(v, str):
            # Quote to be safe with special chars
            safe = v.replace('"', '\\"')
            lines.append(f'{k}: "{safe}"')
        elif isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    safe = str(item).replace('"', '\\"')
                    lines.append(f'  - "{safe}"')
        else:
            lines.append(f"{k}: {v}")
    out_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote config: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out", default="configs/v5_pilot.yaml",
        help="Output yaml path (default: configs/v5_pilot.yaml)",
    )
    ap.add_argument(
        "--defaults", action="store_true",
        help="Accept all defaults, no interactive prompt (useful for CI / re-runs)",
    )
    args = ap.parse_args()

    cfg = V5PilotConfig()
    cfg = ask_all(cfg, accept_defaults=args.defaults)

    if not args.defaults:
        confirm = input("Write this config? (y/N) > ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Aborted. No file written.")
            return 1

    out_path = Path(args.out)
    if not out_path.is_absolute():
        # Resolve relative to repo root (parent of scripts/)
        repo_root = Path(__file__).resolve().parents[2]
        out_path = repo_root / out_path
    write_yaml(cfg, out_path)
    print()
    print("Next steps:")
    print(f"  1. Review the config: cat {out_path}")
    print(f"  2. Build the dataset: python3 scripts/fine_tune/build_v5_pilot_dataset.py --config {out_path}")
    print(f"  3. Launch the run:    bash scripts/fine_tune/launch_v5_pilot.sh --config {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
