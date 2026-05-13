# Handoff — agent-memory

## Current Status (2026-05-13)

### Fine-tune v1 SHIPPED — PR #24 merged to main
- Qwen 2.5-3B tool-call LoRA, 80% validator pass rate, 100% on natural prompts.
- PR: https://github.com/metazen11/agent-memory/pull/24 (squash-merged 2026-05-13T21:55:07Z)
- Closes issues #15–#22; leaves #23 open (LM Studio manual verification).
- GGUF at `models/gguf/qwen2.5-3b-toolcalls-q4km.gguf` (SHA `5e174a04…`), 1.8GB Q4_K_M.
- Loaded in LM Studio at `~/.lmstudio/models/mz/qwen2.5-3b-toolcalls/` (use `~/.lmstudio/...` NOT `~/.cache/lm-studio/...`).
- Current v2 work branch: **`feat/v2-finetune-data-pipeline`** (off main).

### Loop bug discovered in v1 — drives the v2 plan
When given vague natural prompts in LM Studio ("find the fire-map codebase"), v1 model emits `<tool_call>` blocks with **empty `arguments`**, gets a generic result back, repeats. Infinite loop until context fills.

Root cause: **83 % of v1 training prompts were synthetic** (`"Call tool 'X' with appropriate arguments"`). The model never had to commit to argument content from a real user prompt. Only 17 % (2,682 rows) came from real Claude conversations. The data was synthesized because the export pipeline didn't have the user prompts linked properly.

### MAJOR FINDING — agent-memory has the right tables, wrong data linkage

The schema for proper turn capture **already exists**:
- `mem_tool_calls` — **54,987 rows** with full `tool_input` JSON args, response previews, success/error flags ✅
- `mem_user_prompts` — only **1,410 rows** across **54 of 500** sessions ❌
- `mem_sessions` — 893 sessions, but most lack matching user_prompts
- `mem_projects` — 686 projects (`agentMemory`, `fire-map.wfca.com`, etc.) with `full_path` ✅

**Result**: 0 tool_calls can be joined back to a same-session user prompt by FK. The user prompt → tool call linkage was never wired up correctly. 446 sessions captured tool calls but dropped the prompt that originated them.

Meanwhile, **`~/.claude/projects/**/*.jsonl` has the full turn history** for every coding session — user msg + assistant msg + `tool_use` blocks + `tool_result` blocks in proper order. The sample fire-map session alone has 2,067 tool_use blocks. Total across all jsonl is likely **50k-100k tool calls with full args + linked user prompts**.

### Lookup/recall gap — surfaces narrative, hides tool args

The recall path agents use at runtime (`/api/observations`, session-start hints, pre-tool-use hints, MCP `search` tool) currently returns observation **summaries**: `title, narrative, facts, concepts, files_*, tool_name, type`. It does **not** surface `tool_input` (the actual arguments).

The data IS populated in `mem_tool_calls.tool_input` (54,987 rows) and `app/dataset_exports.py` uses it for training export, but the runtime recall endpoints don't join to it. Result: an agent can recall "I called Bash" but not "I called Bash with `git log --oneline -20`" — exactly the detail that prevents the empty-args loop bug at the model level.

**Fix as part of v2 work:**
1. Extend `/api/observations` response shape with a `tool_calls: [{name, input, response_preview, created_at}]` array (left-join `mem_tool_calls` by `observation_id`).
2. Update session-start + pre-tool-use hint generators to include 1–3 representative `(tool_name, args)` examples from recalled observations. Agent sees actionable specifics, not just narrative.
3. Same applies to the `mcp__agent-memory__search` MCP tool — its results should include tool args inline so any agent (Claude, Codex, Anvil, the fine-tuned model) gets the same useful detail.

### LM Studio MCP config
Updated `~/.lmstudio/mcp.json`: anvil + agent-memory entries now point at `/Users/mz/_CODING/...` (was Dropbox). **Restart LM Studio** to pick up.

### Fire-map not in Anvil's workspace index
`~/.anvil/workspace_index.json` (generated 2026-05-12) does not include the `_CODING/fire-map.wfca.com/` tree even though it's under the workspace root. Needs `anvil workspace reindex` or explicit root add — Anvil-side configuration, not agent-memory.

---

## Next Session — Start Here

**You are on branch `feat/v2-finetune-data-pipeline` off the merged main.**

**Goal: close the agent-memory data gap, then ship v2 fine-tune.**

The schema is right. The data is wrong. The fix is a one-time backfill from
the Claude jsonl files, plus a small fix to live capture so future sessions
don't have the same gap. Then v2 dataset export becomes a single SQL query.

### Order of operations (gate each before proceeding)

1. **Decide on two small schema additions** (or skip):
   - `mem_projects.git_remote` (nullable text) — dedupes Dropbox-vs-local
     path forks of the same project.
   - `mem_tool_calls.turn_index` (int) — explicit position within session;
     `created_at` will tie at millisecond precision during bulk import.

2. **Write `scripts/backfill/backfill_from_claude_jsonl.py`** (new):
   - Reads every `~/.claude/projects/**/*.jsonl`.
   - Each jsonl is one session. Filename UUID → `mem_sessions.session_id`.
   - For each `user` message → `mem_user_prompts` row.
   - For each assistant `tool_use` block → `mem_tool_calls` row with full
     `tool_input`, linked to the immediately-prior user prompt of the same
     session.
   - For each `tool_result` block → updates that tool_call's response.
   - Resolves `cwd` → `mem_projects` (insert if new).
   - **Default = dry-run.** Reports counts of would-import sessions /
     prompts / tool_calls per project. Idempotent: dedupes on
     `mem_sessions.session_id` (jsonl UUID).

3. **Run dry-run, review numbers together, then `--commit`.**

4. **Audit the live hooks** (`hooks/session-start.js`,
   `hooks/pre-tool-use.js`) — figure out why only 54/500 sessions wrote a
   user_prompt. Likely a missed insert or wrong condition. Fix so future
   sessions capture both halves from turn one.

5. **Fix the lookup/recall surface** (see "Lookup/recall gap" above):
   - Extend `/api/observations` to join `mem_tool_calls` and return
     `tool_calls[].{name, input, response_preview, created_at}`.
   - Update hint generators (`hooks/session-start.js`,
     `hooks/pre-tool-use.js`) to include actual tool args from past calls.
   - Update `mcp_server.py` `search` tool response shape to match.

6. **Write `fine-tune/build_v2_dataset.py`**: single SQL query joining
   `mem_tool_calls` → `mem_user_prompts` → `mem_sessions` → `mem_projects`,
   per-session grouped into multi-turn Qwen 2.5 tool-call format. Outputs
   to `data/processed/qwen25_tools/v2/`. Should produce **25-35k high-
   quality multi-turn rows** vs v1's 16k mostly-synthetic.

7. **Open GitHub issue #25** with this plan. High priority.

8. **Retrain** using the pipeline already on main. 1.0 epoch this time
   (v1 was 0.5). Add tool descriptions to schemas before training.

9. **Add `--anti-loop` flag to `validate_tool_calls.py`** — detects 3
   consecutive identical tool calls and forces text response. Belt-and-
   suspenders inference guard.

### Two anchor docs

- **`docs/fine_tune/PIPELINE_RUNBOOK.md`** — phase-gated training procedure.
- **`docs/fine_tune/FAILURE_MODES.md`** — 10 known failures + fixes.

### Sanity checks before starting

```bash
# Branch & PR state
git status
gh pr view 24 --repo metazen11/agent-memory   # should show MERGED

# DB up?
curl -s http://localhost:3377/api/health | jq

# The gap (key numbers):
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_tool_calls"
# Expect: 54,987 — full args present
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_user_prompts"
# Expect: 1,410 — only 54 sessions; this is the gap
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_sessions"
# Expect: 893
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_projects"
# Expect: 686

# Source of truth — Claude jsonl files:
find ~/.claude/projects -name '*.jsonl' -type f | wc -l
# Expect: ~2,367 (most in special dirs; ~70-90 are real coding sessions)

# v1 fine-tune tests still pass:
.venv-finetune/bin/python -m pytest tests/fine_tune/ -q
# Expect: 24/24 passed
```

### Critical to remember

- `models/` symlinks to Dropbox cold storage. **Quit Dropbox before any new training run** (`osascript -e 'tell application "Dropbox" to quit'`).
- Use `.absolute()` not `.resolve()` for paths under `models/` to avoid symlink chase into Dropbox.
- 24 pytest tests at `tests/fine_tune/` should all pass; run after any change.
- v1 GGUF (`models/gguf/qwen2.5-3b-toolcalls-q4km.gguf`) is the current ship-it artifact; **don't overwrite until v2 is validated**.
- Two gh accounts configured: `wfca-mz` (read-only on `metazen11/agent-memory`) and `metazen11` (push perms). Switch with `gh auth switch --user metazen11` before push/merge ops.

---

## Previous Status (2026-05-12)

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

Full v1 training pipeline now in repo. Reading order:
- `AGENTS.md` — file map.
- `docs/fine_tune/PIPELINE_RUNBOOK.md` — phase-by-phase recipe.
- `docs/fine_tune/FAILURE_MODES.md` — known failures + fixes.
- `docs/fine_tune/LMSTUDIO_INTEGRATION.md` — LM Studio gotchas.

Historical (deprecated): `docs/training_notes.md` — Qwen 3.5 9B and Gemma 4 paths both failed.
