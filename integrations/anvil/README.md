# Agent Memory (Anvil Toggle Pattern)

Install pack:

```bash
./scripts/install-agent-memory-anvil.sh
# or cross-platform:
node scripts/install-agent-memory-anvil.js
```

Use the same environment flag as Claude/Codex:

- `AGENT_MEMORY_HINTS_ENABLED=1` enables lesson and prompt hint injection.
- `AGENT_MEMORY_HINTS_ENABLED=0` disables hint injection.
- `AGENT_MEMORY_SESSION_HINTS_ENABLED` controls session-start hints only (inherits global if unset).
- `AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED` controls pre-tool lesson warnings only (inherits global if unset).

This only controls guidance injection. It should not disable tool-call capture, dataset export, or memory search.

## Example (Node)

```js
function hintsEnabled() {
  const raw = String(process.env.AGENT_MEMORY_HINTS_ENABLED || '1').trim().toLowerCase();
  return !['0', 'false', 'off', 'no'].includes(raw);
}

if (hintsEnabled()) {
  // Inject lesson or memory hints into system/context prompt.
} else {
  // Skip hint injection.
}
```

## Suggested Integration Points

1. Session-start prompt/context builder.
2. Pre-tool lesson warning checks.
3. Any automatic tool-hint middleware.

## Per-run Toggle

```bash
AGENT_MEMORY_HINTS_ENABLED=0 anvil ...
AGENT_MEMORY_HINTS_ENABLED=1 anvil ...
```

Wrapper (loads `.env` + `.anvil/agent-memory.env` first):

```bash
./scripts/anvil-agent-memory.sh anvil
# windows:
scripts\\anvil-agent-memory.cmd anvil
# cross-platform:
node scripts/anvil-agent-memory.js anvil
```
