#!/usr/bin/env python3
"""Synthesize abstain-on-vague rows for v4.5 training (Change B).

v4 fabricates PR numbers, branches, file lists on vague prompts ('deploy
this', 'let's finish'). Without counter-examples in training, the only
signal is 'elaborate from distribution.'

This script emits ~200 jsonl rows with the v4 dataset's chat shape:
  system  — same SYSTEM_PROMPT as the rest of the dataset
  user    — a short vague prompt
  assistant (tool_call) — ONE discovery tool call (Bash gh status, gh pr list,
                          git log, ls, etc.) OR a Read of a likely orientation
                          file (README, HANDOFF, .claude/settings.json)
  tool    — a synthesized tool_response showing realistic output
  assistant (text)  — short reply asking for clarification, OR summarizing
                      what was found in the discovery output, WITHOUT
                      inventing PR numbers / branches / files

Output:
  data/processed/qwen3_tools/v4.5/abstain.synth.jsonl

The v4.5 builder reads this file (when present) and concatenates it onto
the train split before oversampling. Deterministic — same set every time.

Run:
  .venv-finetune/bin/python scripts/fine_tune/synth_abstain_rows.py --write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT = REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v4.5" / "abstain.synth.jsonl"

SYSTEM_PROMPT = (
    "You are Qwen, created by Alibaba Cloud. You are a helpful assistant "
    "with access to tools."
)

# Tool envelope kept compact and stable — same shape as the rest of
# the v4 dataset rows.
TOOLS_ENVELOPE = [
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
]


# Each scenario:
#   prompt    : the vague user input
#   discovery : (tool_name, args, simulated_response)
#   reply     : the abstain / grounded reply text (short, no fabrication)
#
# Discovery actions are chosen to match real Claude Code patterns. Replies
# either ask for a missing referent OR summarize ONLY what discovery
# returned (no extrapolation).
SCENARIOS: list[dict] = [
    # --- "deploy this" family
    {
        "prompt": "deploy this",
        "discovery": ("Bash", {"command": "git status --short && git log --oneline -3"},
                      "M  app/routes/observations.py\n?? new_feature.py\nb88667b feat: pipeline hardening"),
        "reply": "There are uncommitted changes and one untracked file. Before I can help deploy, I need to know: which service or branch are you deploying to, and should the local changes be committed or stashed first?",
    },
    {
        "prompt": "ship it",
        "discovery": ("Bash", {"command": "git branch --show-current && git status --short"},
                      "feat/v4.5-sprint\nM  some_file.py"),
        "reply": "We're on branch `feat/v4.5-sprint` with uncommitted changes. To ship I need to know: target branch (dev / main) and whether 'it' refers to the current branch's diff or a specific commit range. Which?",
    },
    {
        "prompt": "push",
        "discovery": ("Bash", {"command": "git status --branch --short"},
                      "## feat/v4.5-sprint\nM  file.py"),
        "reply": "Branch `feat/v4.5-sprint` has uncommitted changes. Do you want me to commit them first, or push as-is (no commit yet means nothing to push)?",
    },
    # --- "create a PR" family
    {
        "prompt": "create a pr",
        "discovery": ("Bash", {"command": "gh pr list --json number,title,headRefName --limit 10"},
                      "[]"),
        "reply": "No open PRs on this repo. To create one I need: target base branch and a title/body. What's the change about and which base?",
    },
    {
        "prompt": "open a pr for our work",
        "discovery": ("Bash", {"command": "git log origin/dev..HEAD --oneline"},
                      "d4a9df6 feat: harder eval harness\n1a7034d feat: eval scenarios"),
        "reply": "Two commits on this branch ahead of dev: harder eval harness + eval scenarios. Should the PR target dev or main, and what title would you like?",
    },
    # --- "let's finish" family
    {
        "prompt": "let's finish",
        "discovery": ("Read", {"file_path": "todo.json"},
                      '[{"id": 5, "subject": "v4.5 retrain", "status": "in_progress"}]'),
        "reply": "There's one in-progress task: 'v4.5 retrain'. Is that what you'd like to finish, or did you mean a different open item?",
    },
    {
        "prompt": "finish the task",
        "discovery": ("Bash", {"command": "git log -1 --format='%H %s'"},
                      "d4a9df6 feat(fine-tune): harder eval harness"),
        "reply": "The last commit was the harder eval harness. To finish 'the task' I need to know which task you mean — is there an open issue, a HANDOFF entry, or a specific feature you want to wrap up?",
    },
    {
        "prompt": "wrap up",
        "discovery": ("Bash", {"command": "git status --short"},
                      "M  scripts/fine_tune/build_v4_5_dataset.py"),
        "reply": "One file modified: `scripts/fine_tune/build_v4_5_dataset.py`. Want me to commit + push it, or are there other changes I should look for first?",
    },
    # --- "what's going on" / situational
    {
        "prompt": "what's open",
        "discovery": ("Bash", {"command": "gh issue list --state open --limit 5"},
                      "no open issues"),
        "reply": "No open issues on this repo. If you meant open PRs, branches, or local TODOs, let me know which.",
    },
    {
        "prompt": "status",
        "discovery": ("Bash", {"command": "git status --short --branch"},
                      "## feat/v4.5-sprint...origin/dev\nM  app/routes/prompts.py"),
        "reply": "On `feat/v4.5-sprint` with one modified file (`app/routes/prompts.py`). The branch is ahead of dev. Anything specific you want to know about?",
    },
    {
        "prompt": "where are we",
        "discovery": ("Bash", {"command": "pwd && git log -1 --format='%H %s'"},
                      "/Users/mz/_CODING/agentMemory\nd4a9df6 feat(fine-tune): harder eval harness"),
        "reply": "In `/Users/mz/_CODING/agentMemory`. Last commit is the harder eval harness. Want me to summarize what's changed since the last main merge, or something more specific?",
    },
    # --- "test it" / "verify"
    {
        "prompt": "test it",
        "discovery": ("Bash", {"command": "ls tests/"},
                      "fine_tune  test_anti_loop.py  test_build_v2_dataset.py"),
        "reply": "I see a tests/ directory with several files. Which 'it' should I test — there are tests for fine_tune, anti_loop, and build_v2_dataset. Or do you want me to run the full pytest suite?",
    },
    {
        "prompt": "verify it works",
        "discovery": ("Read", {"file_path": "Makefile"},
                      "(file not found)"),
        "reply": "No Makefile to tell me the verification command. What does 'it' refer to — a recent change, a service, a test suite? I can run `pytest`, but I need to know the scope.",
    },
    # --- "fix"
    {
        "prompt": "fix it",
        "discovery": ("Bash", {"command": "git status --short"},
                      ""),
        "reply": "Working tree clean — nothing modified. To fix 'it' I need to know what's broken: a failing test, an error message, an unexpected output?",
    },
    {
        "prompt": "fix the bug",
        "discovery": ("Bash", {"command": "git log --oneline -5"},
                      "d4a9df6 feat\nb88667b feat\n3ef92f1 fix"),
        "reply": "Recent commits don't surface a bug report. Which bug — do you have a failing test output, an error log, or an issue number for me to look at?",
    },
    # --- "continue" family
    {
        "prompt": "continue",
        "discovery": ("Bash", {"command": "git status --short"},
                      "M  scripts/fine_tune/build_v4_5_dataset.py"),
        "reply": "One file in progress: `scripts/fine_tune/build_v4_5_dataset.py`. Continue with that, or did you have something else in mind?",
    },
    {
        "prompt": "next step",
        "discovery": ("Read", {"file_path": "handoff.md"},
                      "v4.5 retrain pending — needs OOD eval slice + dedup"),
        "reply": "Per handoff.md the next step is the v4.5 retrain with OOD eval slice and dedup. Should I start on that or is there a different next step you had in mind?",
    },
    {
        "prompt": "keep going",
        "discovery": ("Read", {"file_path": "todo.json"},
                      '[]'),
        "reply": "No open todos. What would you like me to keep going on — the current edit, a long-running task, or something else?",
    },
    # --- "fix the imports" / vague scope
    {
        "prompt": "clean it up",
        "discovery": ("Bash", {"command": "git diff --stat"},
                      ""),
        "reply": "No diff to clean up. What scope did you mean — recent commits, a specific file, the whole repo?",
    },
    {
        "prompt": "refactor",
        "discovery": ("Bash", {"command": "find . -name '*.py' -newer .git/HEAD | head -5"},
                      ""),
        "reply": "No recently changed Python files to refactor. Which file or module do you want refactored, and toward what — extract a function, rename a thing, split a module?",
    },
    # --- "review"
    {
        "prompt": "review my changes",
        "discovery": ("Bash", {"command": "git diff --stat HEAD~1"},
                      " app/path_normalize.py | 12 +-\n 1 file changed, 11 insertions(+), 1 deletion(-)"),
        "reply": "One file in the last commit: `app/path_normalize.py` (+11/-1). Want me to walk through what changed, look for issues, or check it against a specific concern?",
    },
    {
        "prompt": "review",
        "discovery": ("Bash", {"command": "git log --oneline -1"},
                      "d4a9df6 feat(fine-tune): harder eval harness"),
        "reply": "Review what — the last commit (harder eval harness), a specific file, an open PR? I need a target to focus on.",
    },
    # --- "what should i do"
    {
        "prompt": "what should i do",
        "discovery": ("Read", {"file_path": "handoff.md"},
                      "v4.5 retrain pending"),
        "reply": "Per handoff.md, the immediate pending work is the v4.5 retrain. That's the next concrete thing on the list — is that what you wanted, or a different decision?",
    },
    {
        "prompt": "any ideas",
        "discovery": ("Bash", {"command": "ls docs/"},
                      "fine_tune  training_runs  lessons-enforcement-design.md"),
        "reply": "I can see docs for fine_tune, training runs, and lessons-enforcement. Ideas about what — pick a direction (training tweaks? a doc gap? a feature?) and I can dig in.",
    },
]


def emit_row(scenario: dict) -> dict:
    name, args, response_text = scenario["discovery"]
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": scenario["prompt"]},
            {
                "role": "assistant",
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }],
            },
            {"role": "tool", "name": name, "content": response_text},
            {"role": "assistant", "content": scenario["reply"]},
        ],
        "tools": [t for t in TOOLS_ENVELOPE if t["function"]["name"] == name],
        "source": "synth_abstain",
        "synthetic": True,
        "is_text_synth_candidate": True,
        "multi_turn": True,
        "project_tag": None,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()

    rows = [emit_row(s) for s in SCENARIOS]
    print(f"Generated {len(rows)} abstain rows.")
    if not args.write:
        print(f"DRY-RUN — would write to {OUTPUT}. Re-run with --write.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
