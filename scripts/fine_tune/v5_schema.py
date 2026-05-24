"""v5 row schema + renderer.

A v5 training row is a structured feature set, not a string. The renderer
turns it into the chat template the model sees. Decoupling means:

- The SYSTEM_PROMPT constant can change without touching the builder.
- The TRUSTED_ROOT token can change without touching the builder.
- New per-row features (e.g., last_modified_files) become schema additions,
  not builder rewrites.
- Audit gates validate against the schema, not against text patterns.

Features (per row):
  system_prompt   : constant, configured here
  project_name    : str  ("agentMemory", "anvil", or "unknown")
  project_root    : str  ("<TRUSTED_ROOT>/agentMemory")    or None for unknown
  cwd_relative    : str  ("<TRUSTED_ROOT>/agentMemory/x")  or absolute fallback
  user_prompt     : str  — the user turn
  tool_turns      : list — assistant tool_call / tool response pairs
  final_reply     : str  | None — closing assistant text turn

Renderer output:
  {"messages": [...], "tools": [...], ...metadata}  ready for the trainer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# --- Constants ---------------------------------------------------------------

# The substrate. Every workspace-relative path uses this prefix. Harness
# substitutes the literal local prefix (e.g., /Users/mz/_CODING) at inference.
TRUSTED_ROOT_TOKEN = "<TRUSTED_ROOT>"

# Local prefixes that should be rewritten to TRUSTED_ROOT at row-build time.
# Add new equivalent prefixes here (different machines, mount points, etc.).
LOCAL_WORKSPACE_PREFIXES = (
    "/Users/mz/_CODING/",
    "/Users/mz/_coding/",          # case insensitive on mac, mixed in DB
    "~/_CODING/",
    "~/_coding/",
    "/Users/mz/Dropbox/_CODING/",  # pre-migration data
    "/Users/mz/Dropbox/_coding/",
)

# The real Anvil operating prompt — what runs in production. Used verbatim
# at training time so training behavior matches inference behavior.
SYSTEM_PROMPT = """\
YOU ARE ANVIL - A Coding orchestrator agent - Use agents via the anvil tool to delegate your coding work and conserve context.

When You Write a file, give the user the path. Always display and link files you create or edit.

### CODE TESTING ABSOLUTE LAW
After Coding / Tool Calls
[ ] YOU MUST TEST all code after writing or modifying it
[ ] YOU MUST verify the code runs without errors
[ ] YOU MUST ensure no existing functionality is broken
[ ] YOU MUST confirm linting/type checks pass (if applicable)
[ ] YOU MUST review the output and behavior after execution
[ ] You MUST Commit your changes to the respective account and repository

### File Awareness & Context
[ ] Always read and review all *.md files in the root working directory before starting
[ ] Always read a file before editing it (it may have changed)
[ ] Verify exact file paths before performing any operation
[ ] NEVER hard code paths or values — use canonical settings variables
[ ] Do not assume directory structure — confirm it

### Clarification & Assumptions
[ ] If requirements are unclear, ask before proceeding
[ ] Do not make silent assumptions about functionality or intent
[ ] Confirm edge cases when behavior is ambiguous

### Code Standards & Approach
[ ] Prefer established libraries and frameworks over building custom solutions
[ ] Use canonical naming conventions
[ ] Follow existing project patterns and architecture

### Editing & Change Safety
[ ] Make minimal, targeted changes — avoid unnecessary refactors
[ ] Preserve existing functionality unless explicitly modifying it
[ ] Check for dependencies and side effects before editing

### Debugging Mindset
[ ] Read error messages carefully
[ ] Trace root cause (not just symptoms)
[ ] Fix the underlying issue, not just the output

### Communication & Updates
[ ] Provide updates after each significant action
[ ] Clearly state what was done and what changed
[ ] Note any issues encountered
[ ] Keep communication concise but clear

### Execution Discipline
[ ] Think before acting — do not rush into code changes
[ ] Break problems into steps before implementation
[ ] Validate each step before moving forward

### One-Line Operating Principle
Read first → Think second → Verify paths → Use proven tools → Make minimal changes → TEST EVERYTHING → Communicate clearly\
"""

UNKNOWN_PROJECT = "unknown"


# --- Path normalization ------------------------------------------------------

# Matches any workspace prefix anywhere in a string (not just at start) so
# embedded paths in tool args, command lines, file_paths all get rewritten.
_PREFIX_RE = re.compile(
    "(" + "|".join(re.escape(p) for p in LOCAL_WORKSPACE_PREFIXES) + ")",
    re.IGNORECASE,
)


def normalize_path_string(s: str) -> str:
    """Rewrite any workspace-prefix in s to TRUSTED_ROOT_TOKEN/. System paths
    (outside the workspace) are left untouched."""
    if not s:
        return s
    return _PREFIX_RE.sub(TRUSTED_ROOT_TOKEN + "/", s)


def normalize_json_paths(obj: Any) -> Any:
    """Recurse through dict/list/str, normalizing every string value's paths."""
    if isinstance(obj, str):
        return normalize_path_string(obj)
    if isinstance(obj, dict):
        return {k: normalize_json_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_json_paths(v) for v in obj]
    return obj


def project_root_token(project_name: str) -> Optional[str]:
    """Returns <TRUSTED_ROOT>/{name}, or None for unknown."""
    if project_name == UNKNOWN_PROJECT:
        return None
    return f"{TRUSTED_ROOT_TOKEN}/{project_name}"


# --- Row schema --------------------------------------------------------------

@dataclass
class ToolTurn:
    """One assistant tool_call → tool response pair."""
    tool_name: str
    tool_arguments: dict           # already path-normalized
    tool_response: Optional[str]   # already path-normalized


@dataclass
class V5Row:
    """A v5 training row, pre-render."""
    project_name: str
    cwd_relative: str              # already path-normalized
    user_prompt: str
    tool_turns: list[ToolTurn] = field(default_factory=list)
    final_reply: Optional[str] = None
    # subfolder: a folder under project_root that has its own concerns
    # (e.g., "wfca-app" under "fire-map.wfca.com"). Optional refinement;
    # parent project_name remains the stable identity.
    subfolder: Optional[str] = None
    # Provenance — useful for debugging / audit, dropped at render time.
    source_ids: list[int] = field(default_factory=list)
    created_at: Optional[str] = None

    @property
    def project_root(self) -> Optional[str]:
        return project_root_token(self.project_name)


# --- Renderer ----------------------------------------------------------------

def _project_block(row: V5Row) -> str:
    """The [Project] block appended to the system prompt."""
    lines = [
        "",
        "[Project]",
        f"project_name: {row.project_name}",
    ]
    if row.project_root:
        lines.append(f"project_root: {row.project_root}")
    if row.subfolder:
        lines.append(f"subfolder: {row.subfolder}")
    lines.append(f"cwd: {row.cwd_relative}")
    return "\n".join(lines)


def render_row(row: V5Row, tools_envelope: list[dict]) -> dict:
    """Turn a V5Row into a chat-format dict ready for the trainer.

    Output shape matches what the trainer's sample-builder expects:
      {"messages": [{"role": "...", "content": "..." or tool_calls}, ...],
       "tools": [...]}
    """
    full_system = SYSTEM_PROMPT + "\n" + _project_block(row)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": full_system},
        {"role": "user", "content": row.user_prompt},
    ]
    for turn in row.tool_turns:
        messages.append({
            "role": "assistant",
            "tool_calls": [{
                "type": "function",
                "function": {
                    "name": turn.tool_name,
                    "arguments": turn.tool_arguments,
                },
            }],
        })
        if turn.tool_response is not None:
            messages.append({
                "role": "tool",
                "name": turn.tool_name,
                "content": turn.tool_response,
            })
    if row.final_reply:
        messages.append({"role": "assistant", "content": row.final_reply})

    return {
        "messages": messages,
        "tools": tools_envelope,
        "project_tag": row.project_name,
        "multi_turn": len(row.tool_turns) > 1,
        "source_ids": row.source_ids,
    }


def render_jsonl_line(row: V5Row, tools_envelope: list[dict]) -> str:
    return json.dumps(render_row(row, tools_envelope), ensure_ascii=False)


# --- Self-test ---------------------------------------------------------------

if __name__ == "__main__":
    # Sanity check the path normalizer + a sample render.
    samples = [
        "/Users/mz/_CODING/agentMemory/scripts/foo.py",
        "/Users/mz/_coding/anvil/lib.py",
        "/Users/mz/Dropbox/_CODING/etrade/notes.md",
        "/etc/hosts",
        "no path here",
        "cd /Users/mz/_CODING/agentMemory && pytest tests/",
    ]
    print("path normalization sanity:")
    for s in samples:
        print(f"  {s!r}")
        print(f"  -> {normalize_path_string(s)!r}")

    print("\nrender sanity:")
    row = V5Row(
        project_name="agentMemory",
        cwd_relative="<TRUSTED_ROOT>/agentMemory/scripts/fine_tune",
        user_prompt="fix the path normalizer",
        tool_turns=[
            ToolTurn(
                tool_name="Read",
                tool_arguments={"file_path": "<TRUSTED_ROOT>/agentMemory/app/path_normalize.py"},
                tool_response="(file contents)",
            ),
        ],
        final_reply="Done — fixed the regex anchor.",
    )
    tools = [{"type": "function", "function": {"name": "Read"}}]
    out = render_row(row, tools)
    print(json.dumps(out, indent=2, ensure_ascii=False))
