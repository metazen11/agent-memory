"""Install the trusted pull-based CD controller as a macOS login service."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).absolute().parents[1]
STORE = Path.home() / ".local/share/agent-memory"
LABEL = "com.metazen.agent-memory-delivery"


def main():
    if not (STORE / "deployment.json").exists():
        raise SystemExit("Run deploy_release.py --prepare first")
    # Pin the controller itself; editing the shared checkout cannot change CD.
    shutil.copyfile(ROOT / "scripts/deploy_release.py", STORE / "controller.py")
    shutil.copyfile(ROOT / "scripts/wait_for_health.py", STORE / "wait_for_health.py")
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [
            str(ROOT / ".venv/bin/python"),
            str(STORE / "controller.py"),
        ],
        "WorkingDirectory": str(STORE),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        },
        "RunAtLoad": True,
        "StartInterval": 120,
        "ThrottleInterval": 60,
        "StandardOutPath": str(STORE / "delivery.log"),
        "StandardErrorPath": str(STORE / "delivery.log"),
    }
    plist.write_bytes(plistlib.dumps(config))
    plist.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(plist)], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    print("Installed", LABEL)


if __name__ == "__main__":
    main()
