# Handoff (agentMemory -> Claude + Codex integration)

## Current status

- Claude integration already existed in repo and remains unchanged.
- Codex integration has been added in separate files (no patch to `install.js`).
- Codex MCP registration was performed and verified with `codex mcp get agent-memory`.

## What was added

- `install-codex.js`
- `scripts/install-agent-memory-codex.sh`
- `scripts/codex-agent-memory.sh`
- `codex.agent-memory.md`
- `integrations/codex/common.js`
- `integrations/codex/session-start.js`
- `integrations/codex/session-end.js`
- `integrations/codex/pre-tool-trigger.js`
- `integrations/codex/post-tool-hook.js`
- `integrations/codex/drain-spool.js`
- `integrations/codex/host-watch.js`
- `integrations/codex/hooks.json`

Also updated:
- `.gitignore` now includes `.agent-memory-codex/`

## Important behavior implemented

### Claude

- Existing fault-tolerance path is already present:
  - `hooks/session-start.js` -> runs `hooks/ensure-services.js`
  - `hooks/post-tool-use.js` -> recovery trigger to `hooks/ensure-services.js`

### Codex (sandbox-safe mode)

- `session-start`:
  - tries API path
  - if unavailable, falls back quickly to local snapshot/spool mode
  - avoids long hangs in sandbox
- `pre-tool-trigger`:
  - uses API when available
  - falls back to `.agent-memory-codex/lessons.snapshot.json`
- `post-tool-hook`:
  - queues online when possible
  - otherwise spools payloads into `.agent-memory-codex/spool/`
- `host-watch` (wrapper-launched):
  - tries to keep services awake via `hooks/ensure-services.js`
  - drains spool back to API
  - refreshes local snapshots

## Known blocker / environment note

- In this Codex sandbox, FastAPI startup fails to connect to external Postgres (`localhost:5433`) with:
  - `PermissionError: [Errno 1] Operation not permitted`
- So online API checks from sandbox may fail. The new Codex path is designed to continue in snapshot/spool mode.
- Running from host terminal (outside restrictive sandbox) should allow full recovery behavior.

## Resume commands

1) Refresh Codex integration:

```bash
./scripts/install-agent-memory-codex.sh
```

2) Start/verify backend services from host terminal:

```bash
node install.js --start
node install.js --status
```

3) Launch Codex with wrapper:

```bash
./scripts/codex-agent-memory.sh
```

4) Optional manual helpers during session:

```bash
node integrations/codex/pre-tool-trigger.js --tool Bash --input "npm run migrate"
node integrations/codex/post-tool-hook.js --tool Bash --input '{"command":"npm test"}' --output "tests passed"
```

## Verification already done

- Syntax checks passed (`node --check`) on new Codex scripts.
- `codex mcp get agent-memory` showed registered server:
  - command: `/Users/mz/Dropbox/_CODING/agentMemory/.venv/bin/python`
  - args: `/Users/mz/Dropbox/_CODING/agentMemory/mcp_server.py`
- `session-start` returns quickly in offline/sandbox mode and writes:
  - `.agent-memory-codex/session-context.md`
- `post-tool-hook` spools correctly when offline.

## Suggested next step when returning

- Run `node install.js --status` and confirm FastAPI is `ok`.
- If still stopped, inspect `logs/server.log` and start services from host terminal (outside strict sandbox).
