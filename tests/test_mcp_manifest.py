"""Guards on .mcp.json — which is ALSO this repo's published plugin manifest.

Three things have broken this file in production:

1. An unexpandable variable in `command`. Claude Code passes `command` to
   posix_spawn with NO shell, so "${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp.sh"
   failed ENOENT on the literal string and killed agent-memory in EVERY
   project (the marketplace is a directory source pointing at this repo).

2. A machine-specific absolute path. `.claude-plugin/marketplace.json` is
   tracked, so third parties can install this repo — "/Users/mz/..." is an
   immediate ENOENT for anyone else.

3. A relative path. The server is spawned with cwd = the USER'S PROJECT, not
   the plugin root (verified: 16 running instances, each with a different
   cwd), so "./scripts/run_mcp.sh" resolves against the wrong directory.

The form that satisfies all three: `command` is a bare interpreter name and
`args` carries $CLAUDE_PLUGIN_ROOT, expanded either by the client (every
official Anthropic plugin does exactly this) or by the shell at runtime. The
env var IS present in the server's environment — it is only the `command`
string that is never expanded.
"""

import json
import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
MCP_JSON = REPO / ".mcp.json"


def _servers():
    return json.loads(MCP_JSON.read_text())["mcpServers"]


def test_command_has_no_unexpanded_variable():
    """`command` is posix_spawn'd directly — a $VAR there is a literal."""
    for name, spec in _servers().items():
        cmd = spec.get("command", "")
        assert "$" not in cmd, (
            f"{name}: command contains a variable that will NOT be expanded: "
            f"{cmd!r}. Put it in `args` instead."
        )
        assert not cmd.startswith("~"), f"{name}: `~` is not expanded: {cmd!r}"


def test_command_is_portable():
    """No machine-specific path — this repo is installable by third parties."""
    for name, spec in _servers().items():
        cmd = spec.get("command", "")
        assert not cmd.startswith(("/Users/", "/home/", "/private/")), (
            f"{name}: {cmd!r} exists on one machine only, but "
            f".claude-plugin/marketplace.json is tracked, so this ships."
        )


def test_command_is_not_cwd_relative():
    """cwd is the user's project, not the plugin root — relative paths break."""
    for name, spec in _servers().items():
        cmd = spec.get("command", "")
        assert not cmd.startswith("./"), (
            f"{name}: {cmd!r} is cwd-relative, but the server is spawned with "
            f"cwd = the user's project directory, not the plugin root."
        )


def test_command_is_resolvable():
    """The command must be findable: on PATH, or an existing executable."""
    import shutil

    for name, spec in _servers().items():
        cmd = spec.get("command", "")
        assert cmd, f"{name}: empty command"
        if "/" in cmd:
            assert os.path.isfile(cmd), f"{name}: no such file: {cmd!r}"
            assert os.access(cmd, os.X_OK), f"{name}: not executable: {cmd!r}"
        else:
            assert shutil.which(cmd), f"{name}: {cmd!r} is not on PATH"


def test_plugin_root_is_referenced_for_the_real_entrypoint():
    """The launcher must be located via $CLAUDE_PLUGIN_ROOT, not guessed.

    With a bare interpreter as `command`, the actual entrypoint lives in
    `args`. It has to be anchored to the plugin root, or it resolves against
    the user's project directory.
    """
    for name, spec in _servers().items():
        blob = " ".join(spec.get("args", []) or [])
        if "/" in spec.get("command", ""):
            continue  # absolute-path form; covered by the tests above
        assert "CLAUDE_PLUGIN_ROOT" in blob, (
            f"{name}: command is a bare interpreter but args do not anchor to "
            f"$CLAUDE_PLUGIN_ROOT: {blob!r}"
        )


def test_launcher_script_exists():
    """Whatever $CLAUDE_PLUGIN_ROOT/... the args name must exist in the repo."""
    import re

    for name, spec in _servers().items():
        blob = " ".join(spec.get("args", []) or [])
        for rel in re.findall(r'\$(?:\{)?CLAUDE_PLUGIN_ROOT(?:\})?(/[\w./-]+)', blob):
            target = REPO / rel.lstrip("/")
            assert target.is_file(), f"{name}: args reference {rel}, missing at {target}"
            assert os.access(target, os.X_OK), f"{name}: {target} is not executable"
