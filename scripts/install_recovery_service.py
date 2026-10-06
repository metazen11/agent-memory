#!/usr/bin/env python3
"""Install, inspect or remove login-supervised local memory recovery on macOS."""

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
LABELS = ("com.metazen.agent-memory-api", "com.metazen.agent-memory-recovery")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    parser.add_argument("--api-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer uses macOS launchd")
    domain = f"gui/{os.getuid()}"
    directory = Path.home() / "Library/LaunchAgents"
    logs = ROOT / "logs"
    for label in LABELS[:1] if args.api_only else LABELS:
        target = directory / f"{label}.plist"
        if args.action == "status":
            subprocess.run(["launchctl", "print", f"{domain}/{label}"], check=False)
            continue
        if target.exists():
            subprocess.run(
                ["launchctl", "bootout", domain, str(target)],
                capture_output=True,
                check=False,
            )
        if args.action == "uninstall":
            target.unlink(missing_ok=True)
            continue
        node = shutil.which("node")
        python = ROOT / ".venv/bin/python"
        if not node or not python.exists():
            parser.error("Install Node and the repository Python virtualenv first")
        directory.mkdir(parents=True, exist_ok=True)
        logs.mkdir(exist_ok=True)
        api = label == LABELS[0]
        config = {
            "Label": label,
            "ProgramArguments": [
                str(python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "3377",
            ]
            if api
            else [node, str(ROOT / "integrations/codex/recovery-worker.js")],
            "WorkingDirectory": str(ROOT),
            "EnvironmentVariables": {
                "PATH": f"{Path(node).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            },
            "RunAtLoad": True,
            "KeepAlive": True if api else {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "StandardOutPath": str(logs / ("server.log" if api else "recovery.log")),
            "StandardErrorPath": str(logs / ("server.log" if api else "recovery.log")),
        }
        if not api:
            config["StartInterval"] = 60
        manifest = ROOT / "release.json"
        if manifest.exists():
            config["EnvironmentVariables"]["AGENT_MEMORY_RELEASE_SHA"] = json.loads(
                manifest.read_text()
            )["sha"]
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(plistlib.dumps(config))
        temporary.chmod(0o600)
        temporary.replace(target)
        subprocess.run(["plutil", "-lint", str(target)], check=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
        print(f"Installed {label}")


if __name__ == "__main__":
    main()
