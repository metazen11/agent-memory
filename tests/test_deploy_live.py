"""scripts/deploy_live.sh against a temp bare origin + temp live clone.

launchctl is a stub on PATH that logs its argv. Health is a real local HTTP
server (the same readiness check production uses), answering ok or 503.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_live.sh"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo, name):
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def health():
    state = {"ok": True}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if state["ok"] else 503)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"db": {"status": "ok"}, "embeddings": {"status": "ok"}}
                ).encode()
            )

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_port}/api/health"
    yield state
    srv.shutdown()


@pytest.fixture
def env(tmp_path, health):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    live = tmp_path / "live"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "main")
    first = commit(work, "a")
    git(work, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(live))
    git(live, "checkout", "-q", "--detach", "origin/main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "launchctl.calls"
    (bin_dir / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    e = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "LIVE_DIR": str(live),
        "HEALTH_TIMEOUT": "1",
        "HEALTH_URL": health["url"],
    }
    return {
        "env": e,
        "work": work,
        "live": live,
        "calls": calls,
        "first": first,
        "log": tmp_path / "home/.agent-memory/logs/deploy.log",
    }


def run(env):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env["env"], capture_output=True, text=True
    )


def test_noop_when_up_to_date(env):
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert not env["calls"].exists()  # no restart
    assert git(env["live"], "rev-parse", "HEAD") == env["first"]


def test_fast_forward_and_restart_when_healthy(env):
    new = commit(env["work"], "b")
    git(env["work"], "push", "-q", "origin", "main")
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert git(env["live"], "rev-parse", "HEAD") == new
    assert "kickstart -k" in env["calls"].read_text()
    log = env["log"].read_text()
    assert env["first"] in log and new in log


def test_rolls_back_when_unhealthy(env, health):
    health["ok"] = False
    commit(env["work"], "b")
    git(env["work"], "push", "-q", "origin", "main")
    r = run(env)
    assert r.returncode == 1
    assert git(env["live"], "rev-parse", "HEAD") == env["first"]
    assert env["calls"].read_text().count("kickstart -k") == 2  # deploy + rollback
    assert "ROLLBACK" in env["log"].read_text()


def test_schedule_plist_points_at_versioned_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "install_deploy_schedule", SCRIPT.with_name("install_deploy_schedule.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    plist = mod.render(Path("/h"))
    assert plist["Label"] == "com.metazen.agent-memory-deploy"
    assert plist["StartInterval"] == 300
    assert plist["ProgramArguments"] == [str(SCRIPT)]
