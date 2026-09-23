"""Regression tests for file_scope lesson delivery through the PreToolUse hook.

Context: every lesson created in the 2026-09-23 session used
trigger_on='file_scope'. hooks/pre-tool-use.js only ever queried the server
with the default trigger_on='input', so /api/lessons/match filtered them out
with `WHERE l.trigger_on = 'input'` and the hook reported "no active lessons
match" for a file the lesson explicitly named.

The bug was invisible because the API was verified directly while the hook
path — the only path that reaches a live session — was never exercised. That
is the same write-path/read-path seam that hid the Anvil middleware bug, so
these tests assert through the hook, not the API.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "pre-tool-use.js"
BASE_URL = "http://localhost:3377"


def _run_hook(payload: dict) -> dict:
    """Invoke the real hook the way Claude Code does: JSON on stdin."""
    proc = subprocess.run(
        ["node", str(HOOK)],
        input=json.dumps(payload),
        text=True,
        cwd=str(ROOT),
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout or "{}")


def _fresh_session() -> str:
    # The hook emits its "no lessons matched" reminder only once per
    # session_id, so every case needs its own id to stay deterministic.
    return f"test-filescope-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def file_scope_lesson():
    """Create a uniquely-named file_scope lesson, delete it afterwards."""
    marker = uuid.uuid4().hex[:10]
    filename = f"zz-{marker}-fixture.json"
    payload = {
        "title": f"file_scope fixture {marker}",
        "rule": f"FIXTURE-{marker}: this lesson must reach the hook.",
        "severity": "critical",
        "trigger_on": "file_scope",
        "trigger_files": [filename, f"**/{filename}"],
    }
    with httpx.Client(base_url=BASE_URL, timeout=10.0,
                      headers={"X-Agent-Name": "claude"}) as c:
        resp = c.post("/api/lessons", json=payload)
        resp.raise_for_status()
        lesson = resp.json()
    try:
        yield {"id": lesson["id"], "marker": marker, "filename": filename}
    finally:
        with httpx.Client(base_url=BASE_URL, timeout=10.0,
                          headers={"X-Agent-Name": "claude"}) as c:
            c.patch(f"/api/lessons/{lesson['id']}", json={"active": False})


def test_edit_absolute_path_delivers_file_scope_lesson(file_scope_lesson):
    """An Edit on an absolute path matches a basename-glob file_scope lesson."""
    out = _run_hook({
        "session_id": _fresh_session(),
        "cwd": str(ROOT),
        "tool_name": "Edit",
        "tool_input": {
            "file_path": f"{ROOT}/{file_scope_lesson['filename']}",
            "old_string": "a",
            "new_string": "b",
        },
    })
    assert file_scope_lesson["marker"] in out.get("systemMessage", ""), (
        "file_scope lesson did not reach the hook — pre-tool-use.js is "
        "probably querying only trigger_on='input' again"
    )


def test_bash_command_delivers_file_scope_lesson(file_scope_lesson):
    """A Bash command touching the file also matches.

    Guards the token-extraction regex: an earlier version consumed the
    leading dot of `.mcp.json`, yielding `mcp.json` and silently failing
    basename matching.
    """
    out = _run_hook({
        "session_id": _fresh_session(),
        "cwd": str(ROOT),
        "tool_name": "Bash",
        "tool_input": {"command": f"sed -i s/a/b/ {file_scope_lesson['filename']}"},
    })
    assert file_scope_lesson["marker"] in out.get("systemMessage", "")


def test_dotfile_keeps_leading_dot_in_bash_extraction():
    """Token extraction preserves a leading dot (regression: `.mcp.json`)."""
    out = _run_hook({
        "session_id": _fresh_session(),
        "cwd": str(ROOT),
        "tool_name": "Bash",
        "tool_input": {"command": "sed -i s/a/b/ .mcp.json"},
    })
    msg = out.get("systemMessage", "")
    # Lesson 48 is the shipped CLAUDE_PLUGIN_ROOT lesson scoped to .mcp.json.
    assert "CLAUDE_PLUGIN_ROOT" in msg, (
        "editing .mcp.json via Bash should surface the CLAUDE_PLUGIN_ROOT lesson"
    )


def test_unrelated_file_does_not_match(file_scope_lesson):
    """No false positives: an unrelated file must not surface the lesson."""
    out = _run_hook({
        "session_id": _fresh_session(),
        "cwd": str(ROOT),
        "tool_name": "Edit",
        "tool_input": {
            "file_path": f"{ROOT}/totally-unrelated-file.txt",
            "old_string": "a",
            "new_string": "b",
        },
    })
    assert file_scope_lesson["marker"] not in out.get("systemMessage", "")
