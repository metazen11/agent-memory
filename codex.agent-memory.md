# Agent Memory (Codex Adapter)

This repo uses `agent-memory` for persistent cross-session memory.

## MCP

Install or refresh the Codex integration:

```bash
./scripts/install-agent-memory-codex.sh
```

Use MCP server `agent-memory` for:
- `search`
- `timeline`
- `get_observations`
- `save_memory`

## Session Start

If launched via `scripts/codex-agent-memory.sh`, a session is already created and context is written to:
- `.agent-memory-codex/session-context.md`

The wrapper also starts a host-side watcher (outside Codex sandbox) that:
- restarts `agent-memory` services if they sleep
- drains locally spooled tool events to `/api/queue`
- refreshes lesson/recent-memory snapshots used by sandbox-safe fallback mode

At the beginning of the session, read `.agent-memory-codex/session-context.md` and briefly acknowledge relevant recent memory before continuing.

## Trigger Checks (Lesson Warnings)

Before risky `Bash`, `Edit`, or `Write` operations, run:

```bash
node integrations/codex/pre-tool-trigger.js --tool Bash --input "npm run migrate"
```

If the API is unavailable, this falls back to `.agent-memory-codex/lessons.snapshot.json`.
If lessons are returned, follow them before proceeding.

## Post-Tool Capture Hook (Manual)

Codex CLI does not currently expose native lifecycle hooks like Claude Code. To record key tool actions, call:

```bash
node integrations/codex/post-tool-hook.js --tool Bash --input '{"command":"npm test"}' --output "tests passed"
```

If the API is unavailable (or sandbox blocks localhost), the payload is spooled to `.agent-memory-codex/spool/` and the host watcher will upload it when services are back.

## Session End

If using the wrapper, session end is automatic. Otherwise run:

```bash
node integrations/codex/session-end.js
```
