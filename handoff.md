# Handoff — agent-memory

## Current Status (2026-05-12)

### Location Change
- **Moved from** `~/Dropbox/_CODING/agentMemory/` **to** `~/_CODING/agentMemory/`
- `models/` and `.venv-finetune/` are symlinks back to Dropbox (79GB cold storage)
- Claude hook symlinks at `~/.claude/hooks/agent-memory-*.js` updated to new path
- Anvil `.mcp.json` updated to new path

### Security Sprint (issues #1-#14)
Shipped in 8 commits to main. Key changes:

**Auth system:**
- `REQUIRE_AUTH=true` in `.env` enables Bearer token auth on all endpoints
- `TRUSTED_AGENTS=anvil,claude,codex,gemini,python-httpx` bypasses auth for known localhost callers
- Hooks send `X-Agent-Name: claude` header for trusted bypass
- Token CLI: `python -m app.cli setup` generates tokens for default agents
- Token management: `python -m app.cli create-token|list-tokens|revoke-token`

**Currently auth is ON** (`REQUIRE_AUTH=true`) with trusted agents bypass active.

**Other security:**
- Host bound to `127.0.0.1` (was `0.0.0.0`)
- `trust_remote_code` removed from embeddings (configurable via `EMBEDDING_TRUST_REMOTE_CODE`)
- CORS middleware locked to localhost origins
- Rate limiting enabled (100 writes/min, 500 reads/min)
- Audit logging enabled (writes_only mode)
- Secret redaction enabled by default (`REDACT_SECRETS=true`)
- PG trust auth warning on startup

**New features:**
- `GET/POST /api/prompts` — searchable user prompt history (1,410 indexed)
- `POST /api/prompts/search` — FTS search over prompts
- Migrations 008-011 auto-apply on startup

### Known Issue — PreToolUse Hook Error in Claude
The `pre-tool-use.js` hook may show errors in Claude sessions started before the auth changes. **Fix: restart Claude Code session** so the updated hook code and env vars load.

If error persists after restart, check:
1. Symlinks point to `~/_CODING/` not `~/Dropbox/`: `ls -la ~/.claude/hooks/agent-memory-*.js`
2. Service is running: `curl http://localhost:3377/api/health`
3. Auth bypass works: `curl -H 'X-Agent-Name: claude' http://localhost:3377/api/prompts?limit=1`

### GitHub Issues
14 issues at metazen11/agent-memory. Closed by commits: #1-#8, #12, #13. Remaining:
- #9 TLS/HTTPS support (low priority)
- #10 Data retention/purge policy (medium)
- #11 Web UI — existing `archive/memory-explorer.html` in anvil repo ready to integrate
- #14 Move off Dropbox on second Mac

## Setup on New Machine

```bash
# 1. Clone/pull
cd ~/_CODING/agentMemory && git pull

# 2. Install deps
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Run migrations (auto on startup)
.venv/bin/uvicorn app.main:app --port 3377 --host 127.0.0.1

# 4. Generate tokens
.venv/bin/python -m app.cli setup

# 5. Set env
echo 'REQUIRE_AUTH=true' >> .env
echo 'export AGENT_MEMORY_TOKEN="<claude-token-from-step-4>"' >> ~/.zshenv

# 6. Update Claude hook symlinks
cd ~/.claude/hooks
ln -sf ~/_CODING/agentMemory/hooks/session-start.js agent-memory-session-start.js
ln -sf ~/_CODING/agentMemory/hooks/session-end.js agent-memory-session-end.js
ln -sf ~/_CODING/agentMemory/hooks/pre-tool-use.js agent-memory-pre-tool-use.js
ln -sf ~/_CODING/agentMemory/hooks/post-tool-use.js agent-memory-post-tool-use.js
ln -sf ~/_CODING/agentMemory/hooks/ensure-services.js agent-memory-ensure-services.js
```

## Fine-tune/Training State

See [docs/training_notes.md](docs/training_notes.md) for full details (models, logs, commands, warnings).

**Summary:** Qwen 9B path is recommended. Gemma 4 has tensor mismatch issues. `models/` is a symlink to Dropbox cold storage — only needed for fine-tuning, not runtime.

## Feature Toggles

- `AGENT_MEMORY_HINTS_ENABLED` (global default)
- `AGENT_MEMORY_SESSION_HINTS_ENABLED` (session-start hints)
- `AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED` (pre-tool warnings)
- Terminal toggle interface: `node scripts/hints-config.js status|set|tui`
- Cross-platform install packs for Claude/Codex/Anvil: `.sh`, `.js`, and Windows `.cmd` launchers.

## Resume Commands

### Service & Auth

```bash
# Check service health
curl http://localhost:3377/api/health

# List tokens
python -m app.cli list-tokens

# Search prompts
curl -H 'X-Agent-Name: claude' 'http://localhost:3377/api/prompts/search' \
  -H 'Content-Type: application/json' -d '{"query":"auth","limit":5}'

# Check toggle state
node scripts/hints-config.js status
```

### Training

See [docs/training_notes.md](docs/training_notes.md) for all training commands, merge/GGUF steps, and warnings.
