# ADR 0001 — agent-memory MCP: single source of truth via Claude plugin

- Status: Accepted
- Date: 2026-06-04
- Deciders: MZ
- Branch implementing this ADR: `fix/agent-memory-mcp-single-source-of-truth`

## Context

`agent-memory` is consumed from Claude Code through an MCP server (stdio
transport, Python launched by `scripts/run_mcp.sh`). The repo root carries
a `.mcp.json` that serves **two** roles simultaneously, which made the
duplicate-registration bug below initially look like a repo problem:

1. **Plugin manifest** — `.claude-plugin/plugin.json` declares
   `"mcpServers": "./.mcp.json"`. When the plugin loader installs
   `agent-memory@metazen11-tools` it reads this file to learn the MCP
   command. The loader sets `CLAUDE_PLUGIN_ROOT` to the plugin cache dir
   before launching, so the literal `${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp.sh`
   command string resolves correctly. This is the **only** path Claude
   should be using.
2. **Auto-detected project MCP** — when a Claude session's cwd is this
   repo, Claude *also* sees the same `.mcp.json` as a *project-scoped* MCP
   registration. Project scope does not set `CLAUDE_PLUGIN_ROOT`, so the
   command string was passed to the shell as a literal and the launcher
   exited with `command not found`. Claude reported `agent-memory ✗ Failed
   to connect` plus a diagnostic warning, every session, every reboot.

Both registrations referred to the same logical server. The duplicate was
a **user-side config bug**, not a repo bug: the project's
`.claude/settings.local.json` had `enabledMcpjsonServers: ["agent-memory"]`
and `enableAllProjectMcpServers: true`, which is what told Claude to treat
the repo's `.mcp.json` as a project-scope registration in the first place.

Separately: Anvil (`/Users/mz/Dropbox/_CODING/anvil`) consumes agent-memory
over HTTP at `http://localhost:3377` (uvicorn process, separate venv,
Postgres-backed). That integration is unrelated to the MCP layer and was
unaffected by the duplicate-registration bug.

## Decision

**Claude consumes agent-memory exclusively through the plugin scope.** The
repo's `.mcp.json` stays in place because the plugin manifest depends on
it (deleting it would break every fresh install of
`agent-memory@metazen11-tools`). The host-side fix is to stop Claude from
*also* loading that same file as a project-scope MCP.

Concretely, the project's `.claude/settings.local.json` no longer carries
`enabledMcpjsonServers` or `enableAllProjectMcpServers`. With those keys
gone Claude does not auto-load `.mcp.json` as a project MCP, the broken
duplicate entry disappears from `claude mcp list`, and the plugin entry
remains the single connected source.

This is reboot-safe because:

- `agent-memory@metazen11-tools` is enabled in `~/.claude/settings.json`,
  which is read on every session start.
- The plugin loader sets `CLAUDE_PLUGIN_ROOT` deterministically before
  spawning the MCP command, so the relative path resolves on every launch.
- `scripts/run_mcp.sh` independently locates the Python venv via, in order:
  `$AGENT_MEMORY_VENV_PYTHON`, `$CLAUDE_PLUGIN_ROOT/.venv/bin/python`, a
  `.source-path` pointer if the plugin author wrote one, and finally
  `$HOME/_CODING/agentMemory/.venv/bin/python`. This survives plugin cache
  pruning that drops symlinked venvs.

Anvil's HTTP integration is unchanged and out of scope for this ADR.

## Consequences

**Positive:**

- One registration, one process, one connected status line. No more
  `Failed to connect` noise at session start, no more diagnostic warning.
- Reboot survival is guaranteed by the plugin loader; nothing depends on a
  shell-exported `CLAUDE_PLUGIN_ROOT`.
- Clear ownership: the plugin manifest is the contract. Changes to launch
  command, env, or transport go through `metazen11/agent-memory` releases,
  not ad-hoc per-repo MCP files.
- Repo stays portable: `.mcp.json` keeps the plugin-loader-aware
  `${CLAUDE_PLUGIN_ROOT}` reference, so any host that installs the plugin
  gets a working MCP regardless of where the cache lands. No host-specific
  absolute paths checked in.

**Negative / accepted trade-offs:**

- If a future developer re-adds `enabledMcpjsonServers: ["agent-memory"]`
  or `enableAllProjectMcpServers: true` to this repo's
  `.claude/settings.local.json`, the duplicate failure returns. This file
  is gitignored, so the risk is per-clone, not committed. A short note in
  `AGENTS.md` should warn against it.
- Disabling the plugin (`agent-memory@metazen11-tools: false` in
  `~/.claude/settings.json`) silently removes MCP wiring. Mitigation: the
  health probe in §Verification fails loudly when the server is gone.

## Verification

The MCP server is healthy if it speaks the protocol when launched directly.
Run from any cwd; this bypasses Claude entirely and proves the launcher,
venv, and Python process all work:

```bash
(cat <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF
sleep 2) | /Users/mz/_CODING/agentMemory/scripts/run_mcp.sh | head
```

Expected: an `initialize` result naming `agent-memory` with a version
string, followed by a `tools/list` result enumerating `recall`, `search`,
`timeline`, `get_observations`, `save_memory`, `create_lesson`,
`search_lessons`, `abilities_memory`, `memory_search_guide`,
`export_training_dataset`, `training_export_guide`.

To verify the Claude-side registration:

```bash
claude mcp list | grep agent-memory
```

Expected: exactly one line, `plugin:agent-memory:agent-memory ... ✓ Connected`,
and no `[Warning] [agent-memory]` diagnostic at the bottom.

## Alternatives considered

- **Delete the repo's `.mcp.json`.** Rejected after codex review caught the
  hidden dependency: `.claude-plugin/plugin.json` references this file as
  the plugin manifest. Deleting it would silently break every fresh plugin
  install — the duplicate-registration noise is host-side, but the file
  itself is load-bearing for the plugin.
- **Rewrite the project entry's command to an absolute path.** Rejected:
  hardcodes the source author's machine path
  (`/Users/mz/_CODING/agentMemory/...`) into a file that ships as the
  plugin manifest. Breaks every other host. Also still leaves two
  `✓ Connected` registrations exposing the same 12 tool names, which is
  the noisy state the project was trying to escape.
- **Move the plugin manifest to a non-project path
  (e.g. `plugin/mcp.json`) and update `plugin.json` accordingly.**
  Considered but adds complexity for no real gain: the host-side fix in
  this ADR is one settings edit and zero repo file changes, and it solves
  the actual symptom.
- **Move the wiring to a host-level `mcpServers` block in
  `~/.claude/settings.json` instead of the plugin.** Rejected: that is the
  same logical wiring as the plugin but loses the plugin's bundled skills,
  hooks, and update path.
- **Run an HTTP MCP transport instead of stdio.** Out of scope here, but
  worth revisiting if multiple agents on the same host need to share one
  process. Anvil already uses HTTP for the non-MCP API surface.

## Related

- Source: `metazen11/agent-memory`, plugin version 1.1.0, server version
  observed 1.26.0.
- Plugin manifest contract: `.claude-plugin/plugin.json` →
  `"mcpServers": "./.mcp.json"` → `scripts/run_mcp.sh`.
- Memory note: `project_agent_memory_mcp_source_of_truth.md` in the user's
  Claude memory store.
