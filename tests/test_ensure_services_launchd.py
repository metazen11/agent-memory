"""ensure-services.js must defer to the launchd API job when one is installed.

Mechanism of the 2026-10-07 outage: the hook only recognised
``com.metazen.agent-memory-api.plist``; the host runs the API under
``com.metazen.agent-memory-server.plist`` (agentMemory-live). The guard was
dead code, so on a transient health failure (postgres still starting at login)
the hook spawned an unmanaged uvicorn from the coding checkout, which won port
3377, and the launchd job crash-looped on EADDRINUSE from then on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DETECT = ROOT / "hooks" / "launchd-api-label.js"


def _detect(launch_agents: Path) -> str:
    proc = subprocess.run(
        ["node", str(DETECT), str(launch_agents)],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "label",
    ["com.metazen.agent-memory-server", "com.metazen.agent-memory-api"],
)
def test_detects_installed_api_supervisor(tmp_path: Path, label: str) -> None:
    (tmp_path / f"{label}.plist").write_text("<plist/>")
    assert _detect(tmp_path) == label


def test_no_supervisor_prints_nothing(tmp_path: Path) -> None:
    (tmp_path / "com.metazen.agent-memory-backup.plist").write_text("<plist/>")
    assert _detect(tmp_path) == ""


def test_ensure_services_has_no_hardcoded_single_label() -> None:
    src = (ROOT / "hooks" / "ensure-services.js").read_text()
    assert "agent-memory-api.plist" not in src, "guard must use launchd-api-label.js"
