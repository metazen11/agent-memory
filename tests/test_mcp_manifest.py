"""Guards on .mcp.json — which is ALSO this repo's published plugin manifest.

Two ways this file breaks, both seen in production:

1. An unexpandable variable. Claude Code passes `command` straight to
   posix_spawn with NO shell, so `${CLAUDE_PLUGIN_ROOT}/...` fails ENOENT on
   the literal string. This shipped and broke agent-memory in every project.

2. A machine-specific absolute path. `.claude-plugin/marketplace.json` is
   tracked, so this repo is installable by third parties — a `/Users/mz/...`
   command is an immediate ENOENT for anyone else.

(2) is currently ACCEPTED for local use and this test documents the exception
explicitly, so it cannot ship to a release unnoticed.
"""

import json
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MCP_JSON = REPO / ".mcp.json"
# The one path knowingly hardcoded for this machine. See docs/adr/0001.
ACCEPTED_LOCAL_PATH = "/Users/mz/_CODING/agentMemory/scripts/run_mcp.sh"


def _commands():
    cfg = json.loads(MCP_JSON.read_text())
    return {name: s.get("command", "") for name, s in cfg["mcpServers"].items()}


def test_no_unexpanded_variables():
    """No `$VAR` or `${VAR}` — posix_spawn never expands them."""
    for name, cmd in _commands().items():
        assert "$" not in cmd, (
            f"{name}: command contains an unexpandable variable: {cmd!r}. "
            "Claude Code spawns this directly with no shell."
        )


def test_no_tilde():
    """`~` is shell syntax too, and equally unexpanded."""
    for name, cmd in _commands().items():
        assert not cmd.startswith("~"), f"{name}: `~` is not expanded: {cmd!r}"


def test_command_exists_and_is_executable():
    """The command must actually be runnable on this machine."""
    for name, cmd in _commands().items():
        assert os.path.isfile(cmd), f"{name}: no such file: {cmd!r}"
        assert os.access(cmd, os.X_OK), f"{name}: not executable: {cmd!r}"


def test_hardcoded_path_is_the_known_exception():
    """A machine-specific path is allowed ONLY as the documented exception.

    If this fails, someone changed the path: either make it portable, or
    update ACCEPTED_LOCAL_PATH deliberately and note it in the ADR.
    """
    for name, cmd in _commands().items():
        if cmd.startswith("/Users/") or cmd.startswith("/home/"):
            assert cmd == ACCEPTED_LOCAL_PATH, (
                f"{name}: unrecognised machine-specific path {cmd!r}. "
                "This ships to third parties via marketplace.json."
            )


@pytest.mark.xfail(
    reason="KNOWN: absolute path is machine-specific and would break third-party "
           "installs. Accepted for local use; must be resolved before publishing "
           "a release. See docs/adr/0001.",
    strict=True,
)
def test_command_is_portable():
    """Release gate: the command must not be machine-specific.

    strict=True means this fails the suite if it ever starts PASSING without
    the xfail being removed — so fixing portability forces this marker to be
    cleaned up rather than silently lingering.
    """
    for name, cmd in _commands().items():
        assert not cmd.startswith(("/Users/", "/home/")), f"{name}: {cmd!r}"
