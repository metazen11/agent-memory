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

## One Middleware Integration

Use `integrations/anvil/middleware.js` as the single source for hint flags and slash-toggle handling in Anvil.

```js
const path = require('path');
const {
  appendSystemInjection,
  logTransparencyMessage,
  resolveHintFlags,
  maybeHandleToolHintsSlashCommand,
} = require('/absolute/path/to/agent-memory/integrations/anvil/middleware');

const ROOT = '/absolute/path/to/agent-memory';
const ENV_FILE = path.join(ROOT, '.env');
const ANVIL_ENV_FILE = path.join(ROOT, '.anvil', 'agent-memory.env');

function middleware(ctx, next) {
  if (maybeHandleToolHintsSlashCommand({
    argv: String(ctx.userText || '').trim().split(/\s+/), // e.g. "/tool-hints off"
    rootDir: ROOT,
    envFile: ENV_FILE,
    anvilEnvFile: ANVIL_ENV_FILE,
  })) {
    return { handled: true };
  }

  const hints = resolveHintFlags({ envFile: ENV_FILE, anvilEnvFile: ANVIL_ENV_FILE });
  if (hints.session) {
    ctx.systemPrompt = appendSystemInjection({
      systemPrompt: ctx.systemPrompt,
      injection: '# Tool Hints\nUse agent-memory lessons and recent memory before tool calls.',
      reason: 'session-hints',
      envFile: ENV_FILE,
      anvilEnvFile: ANVIL_ENV_FILE,
    });
  }
  if (hints.pretool) {
    // Run your pre-tool warning checks here.
  }
  logTransparencyMessage({
    direction: 'inbound',
    role: 'user',
    text: ctx.userText,
    meta: { stage: 'middleware' },
    envFile: ENV_FILE,
    anvilEnvFile: ANVIL_ENV_FILE,
  });
  return next();
}
```

Slash command support:

```bash
./scripts/anvil-agent-memory.sh /tool-hints status
./scripts/anvil-agent-memory.sh /tool-hints on
./scripts/anvil-agent-memory.sh /tool-hints off
./scripts/anvil-agent-memory.sh /tool-hints toggle
./scripts/anvil-agent-memory.sh /tool-hints debug status
./scripts/anvil-agent-memory.sh /tool-hints debug on
./scripts/anvil-agent-memory.sh /tool-hints debug off
```

When debug is on:
- System prompt injections are logged as `[agent-memory:debug]`.
- Full message tracing logs as `[agent-memory:trace]` (preview-truncated).
