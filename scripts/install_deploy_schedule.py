"""Install (or --uninstall) the launchd job that runs scripts/deploy_live.sh every 5 minutes.

Idempotent: re-running rewrites the plist and reloads the job. Run it from the
checkout whose deploy_live.sh should be scheduled (normally the live checkout).
"""

import argparse
import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.metazen.agent-memory-deploy"
INTERVAL_SECONDS = 300
SCRIPT = Path(__file__).resolve().with_name("deploy_live.sh")


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render(home: Path) -> dict:
    log = home / ".agent-memory" / "logs" / "deploy.launchd.log"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(SCRIPT)],
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }


def launchctl(*args: str, check: bool = True) -> None:
    subprocess.run(["launchctl", *args], check=check)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()
    home = Path.home()
    target = plist_path(home)
    domain = f"gui/{os.getuid()}"

    if args.uninstall:
        if target.exists():
            launchctl("bootout", domain, str(target), check=False)
            target.unlink()
            print(f"Uninstalled: {target}")
        else:
            print("Not installed; nothing to remove.")
        return

    if not os.access(SCRIPT, os.X_OK):
        raise SystemExit(f"{SCRIPT} is missing or not executable (chmod +x).")
    target.parent.mkdir(parents=True, exist_ok=True)
    render_log = render(home)["StandardOutPath"]
    Path(render_log).parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plistlib.dumps(render(home)))
    launchctl("bootout", domain, str(target), check=False)
    launchctl("bootstrap", domain, str(target))
    print(f"Installed: {target} (every {INTERVAL_SECONDS}s -> {SCRIPT})")


if __name__ == "__main__":
    main()
