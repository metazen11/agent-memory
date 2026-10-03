"""Exercise real Node hook processes against an isolated HTTP service."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook_host(tmp_path):
    requests = []
    lesson = {
        "id": 999,
        "title": "Scoped hint",
        "rule": "Read the plugin contract first.",
        "severity": "critical",
        "active": True,
        "project_name": str(tmp_path),
    }

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0))) or "null"
            )
            requests.append((self.command, self.path, body))
            parsed = urlparse(self.path)
            if parsed.path == "/api/lessons/match":
                query = parse_qs(parsed.query)
                data = (
                    [lesson]
                    if query.get("trigger_on") == ["file_scope"]
                    and ".mcp.json" in query.get("modified_files", [""])[0]
                    else []
                )
            elif parsed.path == "/api/lessons":
                data = [lesson]
            elif parsed.path.endswith("search"):
                data = {"observations": []}
            else:
                data = {}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        do_GET = do_POST = do_PATCH = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(
        os.environ,
        AGENT_MEMORY_SERVER=f"http://127.0.0.1:{server.server_port}",
        AGENT_MEMORY_CODEX_STATE_DIR=str(tmp_path),
        AGENT_MEMORY_HINTS_ENABLED="1",
        AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED="1",
        AGENT_MEMORY_CODEX_HISTORY=str(tmp_path / "absent-history"),
    )

    def run(script, event, extra_env=None):
        result = subprocess.run(
            ["node", str(ROOT / "integrations/codex" / script)],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=env | (extra_env or {}),
            check=True,
            timeout=10,
        )
        return json.loads(result.stdout)

    yield run, requests, tmp_path
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.mark.parametrize(
    "tool,tool_input",
    [
        (
            "apply_patch",
            {"command": "*** Begin Patch\n*** Update File: .mcp.json\n*** End Patch"},
        ),
        ("Bash", {"command": "cat .mcp.json"}),
    ],
)
def test_file_scope_hints_reach_model(hook_host, tool, tool_input):
    run, requests, tmp = hook_host
    out = run(
        "pre-tool-trigger.js",
        {
            "session_id": "native",
            "cwd": str(tmp),
            "tool_name": tool,
            "tool_input": tool_input,
        },
    )
    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert "plugin contract" in specific["additionalContext"]
    assert out["systemMessage"] == specific["additionalContext"]
    assert {
        parse_qs(urlparse(p).query).get("trigger_on", [""])[0]
        for method, p, b in requests
        if p.startswith("/api/lessons/match")
    } == {"input", "file_scope"}


def test_native_prompt_capture_and_hints(hook_host):
    run, requests, tmp = hook_host
    out = run(
        "user-prompt-submit.js",
        {"session_id": "native", "cwd": str(tmp), "prompt": "Check memory hooks"},
    )
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "plugin contract" in out["hookSpecificOutput"]["additionalContext"]
    assert out["systemMessage"] == out["hookSpecificOutput"]["additionalContext"]
    assert (
        next(b for m, p, b in requests if p == "/api/prompts")["session_id"] == "native"
    )


def test_disabled_hints_still_capture_prompt(hook_host):
    run, requests, tmp = hook_host
    assert (
        run(
            "user-prompt-submit.js",
            {"session_id": "native", "prompt": "hello", "cwd": str(tmp)},
            {"AGENT_MEMORY_HINTS_ENABLED": "0"},
        )
        == {}
    )
    assert any(p == "/api/prompts" for m, p, b in requests)
    assert not any(p.startswith("/api/lessons") for m, p, b in requests)


def test_lifecycle_uses_native_session_not_shared_state(hook_host):
    run, requests, tmp = hook_host
    start = run("session-start.js", {"session_id": "native", "cwd": str(tmp)})
    assert "`native`" in start["hookSpecificOutput"]["additionalContext"]
    state = tmp / ".agent-memory-codex/current-session.json"
    state.write_text(json.dumps({"session_id": "other-chat"}))
    assert run("session-end.js", {"session_id": "native", "cwd": str(tmp)}) == {}
    assert any(m == "PATCH" and p == "/api/sessions/native" for m, p, b in requests)
    assert not any(p == "/api/sessions/other-chat" for m, p, b in requests)


def test_registration_updates_in_place_and_preserves_other_hooks(tmp_path):
    script = """
const {createHostWiring,registerHookEntries}=require('./scripts/lib/agent-memory-host-wiring');
const w=createHostWiring({root:process.cwd(),home:process.argv[1]}).codex;
const fs=require('fs');fs.mkdirSync(require('path').dirname(w.settingsFile),{recursive:true});
fs.writeFileSync(w.settingsFile,JSON.stringify({hooks:{PostToolUse:[{matcher:'other',hooks:[{command:'other'}]}, {...w.hookEntries.find(x=>x.event==='PostToolUse').entry, matcher:'Bash'}]}}));
registerHookEntries(w.settingsFile,w.hookEntries,{updateMatchers:true});
registerHookEntries(w.settingsFile,w.hookEntries,{updateMatchers:true});
console.log(fs.readFileSync(w.settingsFile,'utf8'));
"""
    proc = subprocess.run(
        ["node", "-e", script, str(tmp_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    hooks = json.loads(proc.stdout)["hooks"]
    assert [e["matcher"] for e in hooks["PostToolUse"]] == ["other", "*"]
    assert len(hooks["UserPromptSubmit"]) == 1


def test_prompt_hints_exclude_child_and_sibling_projects(hook_host):
    run, requests, tmp = hook_host
    # Mock service returns the fixture project regardless of the query: the
    # hook must still reject it when cwd is its parent or a sibling prefix.
    for cwd in [tmp.parent, Path(str(tmp) + "-sibling")]:
        out = run(
            "user-prompt-submit.js",
            {"session_id": "native", "cwd": str(cwd), "prompt": "scope check"},
        )
        assert out == {}


def test_offline_snapshot_rejects_foreign_project(hook_host):
    run, requests, tmp = hook_host
    state = tmp / ".agent-memory-codex"
    state.mkdir()
    (state / "lessons.snapshot.json").write_text(
        json.dumps(
            {
                "project_path": "/other/project",
                "lessons": [
                    {
                        "id": 1,
                        "rule": "FOREIGN",
                        "severity": "critical",
                        "project_name": "/other/project",
                        "trigger_tool": "Bash",
                    }
                ],
            }
        )
    )
    assert (
        run(
            "pre-tool-trigger.js",
            {
                "session_id": "native",
                "cwd": str(tmp),
                "tool_name": "Bash",
                "tool_input": {"command": "hello"},
            },
            {"AGENT_MEMORY_SERVER": "http://127.0.0.1:1"},
        )
        == {}
    )


def test_failed_prompt_is_spooled_and_drained_without_session_state(hook_host):
    run, requests, tmp = hook_host
    event = {
        "session_id": "offline-native",
        "cwd": str(tmp),
        "prompt": "Restore the recorder",
    }
    assert (
        run(
            "user-prompt-submit.js",
            event,
            {
                "AGENT_MEMORY_SERVER": "http://127.0.0.1:1",
                "AGENT_MEMORY_HINTS_ENABLED": "0",
            },
        )
        == {}
    )
    spool = tmp / ".agent-memory-codex/spool"
    files = list(spool.glob("*.json"))
    assert len(files) == 1
    envelope = json.loads(files[0].read_text())
    assert envelope["route"] == "/api/prompts"
    assert envelope["payload"]["session_id"] == "offline-native"
    out = run("drain-spool.js", {})
    assert out["drained"] == 1
    assert not list(spool.glob("*.json"))
    assert (
        next(body for method, path, body in requests if path == "/api/prompts")[
            "prompt"
        ]
        == event["prompt"]
    )


def test_patch_matches_both_edit_and_write_lesson_aliases(hook_host):
    run, requests, tmp = hook_host
    run(
        "pre-tool-trigger.js",
        {
            "session_id": "native",
            "cwd": str(tmp),
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Add File: .mcp.json\n*** End Patch"
            },
        },
    )
    names = {
        parse_qs(urlparse(path).query)["tool_name"][0]
        for method, path, body in requests
        if path.startswith("/api/lessons/match")
    }
    assert names == {"apply_patch", "Edit", "Write"}
