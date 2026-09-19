# Agent Memory (Codex Adapter)

This repo uses `agent-memory` for persistent cross-session memory.

## MCP

Install or refresh the Codex integration:

```bash
./scripts/install-agent-memory-codex.sh
# or cross-platform:
node scripts/install-agent-memory-codex.js
```

Use MCP server `agent-memory` for:
- `search`
- `timeline`
- `get_observations`
- `save_memory`

For API-side tool lookup/export (outside MCP), use:
- `GET /api/tool-calls` (lookup)
- `GET /api/tool-calls/stats`
- `GET /api/tool-calls/export?format=jsonl|csv`
- `GET /api/tool-calls/export/dataset` (training-ready datasets)
- `GET /api/tool-calls/export/help` (agent primer)

## Session Start

If launched via `scripts/codex-agent-memory.sh`, a session is already created and context is written to:
- `.agent-memory-codex/session-context.md`

The wrapper also starts a host-side watcher (outside Codex sandbox) that:
- restarts `agent-memory` services if they sleep
- drains locally spooled tool events to `/api/queue`
- refreshes lesson/recent-memory snapshots used by sandbox-safe fallback mode

At the beginning of the session, read `.agent-memory-codex/session-context.md` and briefly acknowledge relevant recent memory before continuing.

Toggle prompt hints on/off:

```bash
export AGENT_MEMORY_HINTS_ENABLED=0   # disable lesson/prompt hint injection
export AGENT_MEMORY_HINTS_ENABLED=1   # re-enable
```

Split toggles:

```bash
export AGENT_MEMORY_SESSION_HINTS_ENABLED=1   # keep session-start hints on
export AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED=0  # disable pre-tool warnings
```

Local TUI-style controller (writes `.env`):

```bash
node scripts/hints-config.js status
node scripts/hints-config.js set session on
node scripts/hints-config.js set pretool off
node scripts/hints-config.js tui
```

## Native Hooks

`install-agent-memory-codex` now wires the same lifecycle surface Codex exposes in
`~/.codex/hooks.json`:

- `SessionStart` starts or resumes an agent-memory session.
- `PreToolUse` checks active lessons for risky tool calls.
- `PostToolUse` records tool calls to `/api/queue`.
- `SessionEnd` marks the session completed.

The installed hook shims live in `~/.codex/hooks/` and symlink back to the
operational checkout. On this machine, that checkout should be
`/Users/mz/_CODING/agentMemory`; do not point Codex at the Dropbox copy.

## Manual Trigger Checks (Fallback)

Before risky `Bash`, `Edit`, or `Write` operations, run:

```bash
node integrations/codex/pre-tool-trigger.js --tool Bash --input "npm run migrate"
```

If the API is unavailable, this falls back to `.agent-memory-codex/lessons.snapshot.json`.
If lessons are returned, follow them before proceeding.
If hints are disabled (`AGENT_MEMORY_HINTS_ENABLED=0`), this command exits with a disabled notice.

## Manual Post-Tool Capture (Fallback)

If native hooks are unavailable, record key tool actions manually:

```bash
node integrations/codex/post-tool-hook.js --tool Bash --input '{"command":"npm test"}' --output "tests passed"
```

If the API is unavailable (or sandbox blocks localhost), the payload is spooled to `.agent-memory-codex/spool/` and the host watcher will upload it when services are back.

## Session End

If using the wrapper, session end is automatic. Otherwise run:

```bash
node integrations/codex/session-end.js
```
