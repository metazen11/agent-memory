"""Exercise real Node hook processes against an isolated HTTP service."""

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
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
    local_policy = {
        "id": 1000,
        "title": "Repository push policy",
        "rule": "Never force-push this repository.",
        "severity": "critical",
        "active": True,
        "project_name": str(tmp_path),
    }
    global_policy = {
        "id": 1001,
        "title": "Global push policy",
        "rule": "Always force-push after rebasing.",
        "severity": "critical",
        "active": True,
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
                query = parse_qs(parsed.query)
                data = [lesson, local_policy] if query.get("project") else [global_policy]
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


def test_native_prompt_overview_is_once_per_session(hook_host):
    run, requests, tmp = hook_host
    event = {"session_id": "once", "cwd": str(tmp), "prompt": "first"}
    first = run("user-prompt-submit.js", event)
    second = run("user-prompt-submit.js", {**event, "prompt": "second"})
    assert "plugin contract" in first["hookSpecificOutput"]["additionalContext"]
    assert second == {}
    assert len([p for m, p, b in requests if urlparse(p).path == "/api/lessons"]) == 2
    lesson_queries = [parse_qs(urlparse(p).query) for m, p, b in requests if urlparse(p).path == "/api/lessons"]
    assert any(q.get("strict_scope") == ["true"] for q in lesson_queries)


def test_native_prompt_project_policy_precedes_conflicting_global_policy(hook_host):
    run, requests, tmp = hook_host
    out = run(
        "user-prompt-submit.js",
        {"session_id": "policy", "cwd": str(tmp), "prompt": "first"},
    )
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "Never force-push this repository" in context
    assert "Always force-push after rebasing" not in context


def test_native_prompt_overview_is_keyed_by_session_not_current_state(hook_host):
    run, requests, tmp = hook_host
    outputs = []
    for session in ("a", "b", "a"):
        outputs.append(run("user-prompt-submit.js", {"session_id": session, "cwd": str(tmp), "prompt": session}))
    assert all("hookSpecificOutput" in outputs[i] for i in (0, 1))
    assert outputs[2] == {}
    lesson_requests = [p for m, p, b in requests if urlparse(p).path == "/api/lessons"]
    assert len(lesson_requests) == 4


def test_native_prompt_concurrent_same_session_only_one_overview(hook_host):
    run, requests, tmp = hook_host
    event = {"session_id": "concurrent", "cwd": str(tmp), "prompt": "same"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: run("user-prompt-submit.js", event), range(2)))
    assert sum("hookSpecificOutput" in out for out in outputs) == 1


def test_repeated_session_start_does_not_reset_lesson_delivery(hook_host):
    run, requests, tmp = hook_host
    event = {"session_id": "restartable", "cwd": str(tmp)}
    run("session-start.js", event)
    run("user-prompt-submit.js", {**event, "prompt": "first"})
    run("session-start.js", event)
    second = run("user-prompt-submit.js", {**event, "prompt": "second"})
    assert second == {}


def test_native_prompt_retries_after_http_failure_without_snapshot(tmp_path):
    lesson = {
        "id": 1001,
        "title": "Recovered hint",
        "rule": "Read the recovered contract.",
        "severity": "critical",
        "active": True,
        "project_name": str(tmp_path),
        "project_id": 7,
    }
    requests = []
    lesson_attempts = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            requests.append(self.path)
            if urlparse(self.path).path == "/api/lessons":
                lesson_attempts["count"] += 1
                if lesson_attempts["count"] == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps([lesson] if parse_qs(urlparse(self.path).query).get("project") else []).encode()
            else:
                body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

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
    )

    def run(prompt):
        result = subprocess.run(
            ["node", str(ROOT / "integrations/codex" / "user-prompt-submit.js")],
            input=json.dumps({"session_id": "retry", "cwd": str(tmp_path), "prompt": prompt}),
            text=True, capture_output=True, cwd=tmp_path, env=env, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    try:
        assert run("first") == {}
        recovered = run("second")
        assert "recovered contract" in recovered["hookSpecificOutput"]["additionalContext"]
        assert lesson_attempts["count"] == 3  # scoped + global on each prompt; first scoped fails
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_native_prompt_empty_success_is_delivered_once(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"[]")

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
    )

    def run(prompt):
        result = subprocess.run(
            ["node", str(ROOT / "integrations/codex" / "user-prompt-submit.js")],
            input=json.dumps({"session_id": "empty", "cwd": str(tmp_path), "prompt": prompt}),
            text=True, capture_output=True, cwd=tmp_path, env=env, check=True, timeout=10,
        )
        return json.loads(result.stdout)

    try:
        assert run("first") == {}
        assert run("second") == {}
        assert len([p for p in requests if p.startswith("/api/lessons")]) == 2
        delivery = json.loads((tmp_path / ".agent-memory-codex" / "lesson-delivery.json").read_text())
        assert delivery["sessions"]["empty"]["status"] == "completed"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_native_prompt_retrieval_error_does_not_complete_from_snapshot(tmp_path):
    state = tmp_path / ".agent-memory-codex"
    state.mkdir()
    (state / "lessons.snapshot.json").write_text(
        json.dumps({"project_path": str(tmp_path), "lessons": [{"id": 7, "title": "stale", "rule": "stale"}]}),
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_GET(self):
            self.send_response(503)
            self.end_headers()

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
    )
    try:
        result = subprocess.run(
            ["node", str(ROOT / "integrations/codex" / "user-prompt-submit.js")],
            input=json.dumps({"session_id": "error", "cwd": str(tmp_path), "prompt": "retry"}),
            text=True, capture_output=True, cwd=tmp_path, env=env, check=True, timeout=10,
        )
        assert json.loads(result.stdout) == {}
        assert not (state / "lesson-delivery.json").exists()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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
    # hook must still reject that foreign lesson. Explicit global lessons are
    # allowed, so the result is not expected to be empty.
    for index, cwd in enumerate([tmp.parent, Path(str(tmp) + "-sibling")]):
        out = run(
            "user-prompt-submit.js",
            {"session_id": f"scope-{index}", "cwd": str(cwd), "prompt": "scope check"},
        )
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "plugin contract" not in context
        assert "Never force-push this repository" not in context


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


@pytest.mark.parametrize(
    "pattern,file",
    [
        ("src/test_?.py", "src/test_a.py"),
        ("src/test_?.py", "src/test_ab.py"),
        ("src/[ab].py", "src/a.py"),
        ("src/[ab].py", "src/c.py"),
        ("[!a-c].py", "z.py"),
        ("[!a-c].py", "b.py"),
        ("[z-a].py", "z.py"),
        ("[!z-a].py", "z.py"),
        ("[]].py", "].py"),
        ("[[]*.py", "[file.py"),
        ("[abc.py", "[abc.py"),
        ("*.py", "src/sub/file.py"),
        ("?.py", "é.py"),
        ("?.py", "😀.py"),
        ("x.py", "x.py\n"),
        ("*", "file\nname"),
    ],
)
def test_offline_file_globs_match_api_fnmatch(hook_host, pattern, file):
    from app.routes.lessons import _any_glob_matches

    run, requests, tmp = hook_host
    state = tmp / ".agent-memory-codex"
    state.mkdir()
    (state / "lessons.snapshot.json").write_text(
        json.dumps(
            {
                "project_path": str(tmp),
                "lessons": [
                    {
                        "id": 1,
                        "rule": "GLOB_HINT",
                        "severity": "warning",
                        "active": True,
                        "project_name": str(tmp),
                        "trigger_on": "file_scope",
                        "trigger_files": [pattern],
                    }
                ],
            }
        )
    )
    out = run(
        "pre-tool-trigger.js",
        {
            "session_id": "native",
            "cwd": str(tmp),
            "tool_name": "Edit",
            "tool_input": {"file_path": file},
        },
        {"AGENT_MEMORY_SERVER": "http://127.0.0.1:1"},
    )
    assert bool(out) == _any_glob_matches([pattern], [file])


def test_archival_scope_comparisons_canonicalize_to_local_working_root():
    script = """
const {lessonAppliesToProject, normalizeProjectPath}=require('./integrations/codex/common');
console.log(JSON.stringify([
 normalizeProjectPath('/Users/mz/Dropbox/_CODING/repo/'),
 lessonAppliesToProject({project_name:'/Users/mz/_CODING/repo'},'/Users/mz/Dropbox/_CODING/repo/subdir'),
 lessonAppliesToProject({project_name:'/Users/mz/_CODING/repo'},'/Users/mz/_CODING/repo-sibling'),
]));
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True
    )
    assert json.loads(result.stdout) == ["/Users/mz/_CODING/repo", True, False]
