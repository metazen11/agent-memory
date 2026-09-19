from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str, *, stdin: dict | None = None, args: list[str] | None = None, cwd: Path | None = None) -> dict:
    proc = subprocess.run(
        ["node", str(ROOT / script), *(args or [])],
        input=json.dumps(stdin) if stdin is not None else None,
        text=True,
        cwd=cwd or ROOT,
        capture_output=True,
        check=True,
    )
    assert proc.stderr == "" or proc.stdout
    return json.loads(proc.stdout)


def test_codex_post_tool_hook_mode_outputs_contract_safe_json(tmp_path: Path) -> None:
    out = _run_node(
        "integrations/codex/post-tool-hook.js",
        stdin={
            "session_id": "hook-contract-post",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_response": "hi",
        },
        cwd=tmp_path,
    )
    assert out == {}


def test_codex_pre_tool_hook_mode_outputs_contract_safe_json(tmp_path: Path) -> None:
    out = _run_node(
        "integrations/codex/pre-tool-trigger.js",
        stdin={
            "session_id": "hook-contract-pre",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
        cwd=tmp_path,
    )
    assert set(out).issubset({"systemMessage"})


def test_codex_session_start_hook_mode_injects_context(tmp_path: Path) -> None:
    out = _run_node(
        "integrations/codex/session-start.js",
        stdin={
            "session_id": "hook-contract-start",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "source": "startup",
        },
        cwd=tmp_path,
    )
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    assert "Agent Memory (Codex)" in specific["additionalContext"]


def test_codex_wiring_registers_session_end_not_stop() -> None:
    script = """
const os = require('os');
const path = require('path');
const { createHostWiring } = require('./scripts/lib/agent-memory-host-wiring');
const wiring = createHostWiring({ root: process.cwd(), home: os.homedir() }).codex;
console.log(JSON.stringify(wiring.hookEntries.map((item) => item.event)));
""".strip()
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    events = json.loads(proc.stdout)
    assert "SessionEnd" in events
    assert "Stop" not in events


def test_codex_post_tool_manual_mode_keeps_diagnostic_output(tmp_path: Path) -> None:
    out = _run_node(
        "integrations/codex/post-tool-hook.js",
        args=[
            "--session", "manual-contract-post",
            "--tool", "Bash",
            "--input", '{"command":"echo hi"}',
            "--output", "hi",
        ],
        cwd=tmp_path,
    )
    assert out["ok"] is True
    assert out["queued"] is True
    assert out["session_id"] == "manual-contract-post"
