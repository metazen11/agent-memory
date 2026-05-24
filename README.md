# agent-memory

A persistent memory layer for Claude Code (and any MCP-compatible agent)
that captures **every user prompt and tool_call** from your coding sessions,
exposes them for recall via an MCP server + FastAPI, and feeds them back
into a local fine-tune pipeline that produces project-specific tool-calling
LoRAs. Built locally, runs locally — Postgres + pgvector, FastAPI on
`127.0.0.1:3377`, GGUFs in LM Studio. The hooks are the recorder; the
mem_* tables are the journal; the fine-tune pipeline is what turns that
journal into a model that actually knows your codebases.

## System overview

```
Claude session ──► hooks (UserPromptSubmit, PreToolUse, PostToolUse, SessionStart/End)
                          │
                          ▼
                   FastAPI ingest (port 3377, Bearer-token auth)
                          │
                          ▼
                   Postgres (mem_user_prompts, mem_tool_calls, mem_sessions,
                             mem_projects, mem_observations, mem_lessons)
                          │
            ┌─────────────┴──────────────┐
            ▼                            ▼
   MCP recall surface          fine-tune pipeline
   (search, timeline,          (build_v3_dataset.py)
    get_observations,                  │
    save_memory, lessons)              ▼
                              LoRA train → merge → GGUF
                                       │
                                       ▼
                                  LM Studio
```

The write path (hooks → FastAPI → queue → Postgres) is fire-and-forget
and never blocks Claude. The read path (MCP tools) uses Reciprocal Rank
Fusion across pgvector and Postgres FTS. The training path reads directly
from `mem_tool_calls` joined to `mem_user_prompts` and emits Qwen-format
chat datasets.

## Repository structure

| Path | Purpose |
|---|---|
| `app/` | FastAPI service (lifecycle, routes, middleware, queue worker, redaction, auth) |
| `app/routes/` | REST endpoints by domain: health, observations, sessions, admin, lessons, prompts, tool-calls |
| `mcp_server.py` | Stdio MCP server. Own DB pool + embedding model, zero FastAPI dependency |
| `hooks/` | Claude Code Node.js hooks (UserPromptSubmit, Pre/PostToolUse, SessionStart/End, ensure-services) |
| `scripts/migrations/` | Versioned SQL migrations (001-initial-schema through 013-project-consolidation) |
| `scripts/fine_tune/` | Training pipeline: dataset builders, validator, smoke tests, GGUF verify, the wizard |
| `scripts/backfill/` | Backfill tool_calls + prompts from Claude JSONL session logs |
| `scripts/` (root) | install_backup_schedule.sh, backup.sh, run_migrations.py, install-agent-memory-*.sh |
| `models/` | Base/LoRA/merged/GGUF artifacts. Symlinked to Dropbox cold storage. Gitignored |
| `data/` | Postgres backups + processed datasets (`processed/qwen25_tools/v2/`, `processed/qwen3_tools/v3/`). Gitignored |
| `tests/` | pytest API integration tests + `tests/fine_tune/` validator/dataset tests + real-world A/B harnesses |
| `docs/` | `fine_tune/` (V3_PLAN, V2_DATA_PIPELINE_PLAN, FAILURE_MODES, WIZARD, PIPELINE_RUNBOOK), `training_runs/`, `backups.md`, `PRIMER.md` |
| `hooks/hooks.json` | Reference hook registration; copy into `~/.claude/settings.json` |
| `install.js` | Legacy Node installer (Docker + native PG, MCP register). Still works for fresh installs |

## Database schema (overview)

All tables are `mem_*`-prefixed to avoid collisions in a shared Postgres.

| Table | Purpose |
|---|---|
| `mem_tool_calls` | Every tool_call captured from Claude sessions — input, output preview, success, errors. The training fuel |
| `mem_user_prompts` | User prompts that drove the tool_calls. Linked from `mem_tool_calls.prev_user_prompt_id` (migration 012) |
| `mem_sessions` | Claude session identifiers + start/end time + final status |
| `mem_projects` | Project identity keyed on git root + remote + branch (post migration 013 consolidation) |
| `mem_observations` | Explicit memory observations (semantic notes the agent or user saved). 768-dim pgvector embeddings |
| `mem_observation_queue` | Async processing queue for the worker (`FOR UPDATE SKIP LOCKED`) |
| `mem_lessons` | Proactive rules triggered before risky tool calls (Edit/Write/Bash/NotebookEdit) |
| `mem_schema_migrations` | Migration history. One row per applied file in `scripts/migrations/` |

Full schema lives in `scripts/migrations/*.sql`. The current head is
`013-project-consolidation.sql`. See `docs/PRIMER.md` for column-level
details and `docs/fine_tune/V2_DATA_PIPELINE_PLAN.md` for the
prompt↔tool_call linkage design introduced by migration 012.

## Hooks — how data gets in

Five Node.js hooks live in `hooks/`. They are designed fire-and-forget
(~30-40ms p99) and exit 0 on every error path so a misconfigured or
down agent-memory never blocks Claude.

| Hook | Event | Description |
|---|---|---|
| `user-prompt-submit.js` | UserPromptSubmit | POSTs prompt text + session + cwd to `/api/prompts`. Live capture of the prompt that drives the next tool calls (added by issue #30, before that mem_user_prompts was empty between 2026-03-29 and 2026-05-13) |
| `pre-tool-use.js` | PreToolUse | Checks active lessons for Edit/Write/Bash/NotebookEdit. Injects warnings as a systemMessage |
| `post-tool-use.js` | PostToolUse | Fire-and-forget POST to `/api/queue`. If the server is down, spawns `ensure-services.js` |
| `session-start.js` | SessionStart | Blocks until services are healthy. Calls `ensure-services.js` if down. Installs daily backup schedule (idempotent) |
| `session-end.js` | Stop | PATCHes `/api/sessions/{id}` to mark the session completed |

Hook auth shares `hooks/auth-header.js` which reads `AGENT_MEMORY_TOKEN`
from the environment. Hooks also send `X-Agent-Name: claude` so the
trusted-agents bypass applies on localhost.

To wire them into Claude Code, symlink each `hooks/*.js` file into
`~/.claude/hooks/` and register the hook list in `~/.claude/settings.json`.
The exact commands are in HANDOFF.md under "Setup on New Machine".

## Fine-tune pipeline status

| Version | Base | Status | Notes |
|---|---|---|---|
| v1 | Qwen2.5-3B-Instruct | shipped, in production | Q4_K_M GGUF at `models/gguf/qwen2.5-3b-toolcalls-q4km.gguf`, loaded in LM Studio. Has a known empty-args loop bug on vague prompts — anti-loop guard mitigates |
| v2 | Qwen2.5-3B-Instruct | **RETRACTED 2026-05-15** | Multi-turn regression in real-world A/B (0/10 useful, 90% re-emit). Eval gate measured the wrong symptom. See `docs/training_runs/v2-real-world-test.md` |
| v3 | Qwen3-4B | in progress | Local MPS training, ≤6 GB Q4_K_M rule, ≥125k effective context via YaRN. Plan doc currently lists Qwen3-8B as the target; the 4B is the smoke/iteration run |

Anchor docs:
- `docs/fine_tune/V3_PLAN.md` — current training plan with multi-turn fixes baked in
- `docs/training_runs/v2-real-world-test.md` — verbatim A/B transcripts that drove the retraction
- `docs/fine_tune/FAILURE_MODES.md` — 12 operational gotchas (resolve()-into-Dropbox, llama-cli hangs, YaRN config, anti-loop, etc.)
- `docs/fine_tune/PIPELINE_RUNBOOK.md` — phase-by-phase recipe
- `docs/fine_tune/V2_DATA_PIPELINE_PLAN.md` — how the v2 dataset shape was built (still the v3 data shape too)

The training script (`models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py`)
is **env-var-driven**, not argparse — set `MODEL_SLUG`, `DATASET_VERSION`,
`DATASET_TIER`, `RUN_TAG`, `EPOCHS`.

## Setup / quickstart

This assumes the legacy installer is not desired. For a one-shot install,
`node install.js` still works (sets up Docker or native Postgres, MCP
registration, hook symlinks, daily backup, .env).

```bash
# 1. Clone
git clone https://github.com/metazen11/agent-memory.git ~/_CODING/agentMemory
cd ~/_CODING/agentMemory

# 2. Python venv (project targets 3.12+; current dev runs 3.14)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Postgres (native Homebrew recommended on macOS)
brew install postgresql@16 pgvector
brew services start postgresql@16
createuser -s mz
createdb -O mz agent_memory
psql -d agent_memory -c "CREATE EXTENSION vector;"

# 4. Configure .env (copy from .env.example, set POSTGRES_* + REQUIRE_AUTH=true)
cp .env.example .env

# 5. Start the API (migrations run on startup)
.venv/bin/uvicorn app.main:app --port 3377 --host 127.0.0.1

# 6. In another shell: generate tokens for trusted agents
.venv/bin/python -m app.cli setup
echo 'export AGENT_MEMORY_TOKEN="<claude-token-from-step-6>"' >> ~/.zshenv

# 7. Symlink hooks into ~/.claude/hooks/ and register them in settings.json
#    Full commands: see HANDOFF.md "Setup on New Machine"

# 8. Install the daily Postgres backup schedule (idempotent)
bash scripts/install_backup_schedule.sh
bash scripts/install_backup_schedule.sh --check
```

Verify the install:

```bash
curl http://localhost:3377/api/health
.venv/bin/python -m app.cli list-tokens
```

## The wizard (operator tool for fine-tunes)

`scripts/fine_tune/wizard.py` is a Textual TUI that sequences the v2/v3
ad-hoc playbook into one command. It runs phase by phase with gates so
a bad dataset or a failed smoke can't silently turn into a 36-hour
training run.

```bash
.venv-finetune/bin/python scripts/fine_tune/wizard.py

# Or, replay a saved config non-interactively
.venv-finetune/bin/python scripts/fine_tune/wizard.py \
    --config train_config.yaml --no-tui
```

Stages: verify env (Dropbox quit, MPS available, disk free) → build
dataset (`build_v2_dataset.py` / `build_v3_dataset.py`) → audit gate
(token counts, tool histogram, drop-reason MANIFEST) → tiny smoke
(200 rows, 1 epoch, ~25-40 min) → tiny validator (≥3% parse rate)
→ full train (~3-4h MPS for 3B, ~36-40h for 8B) → full validator
(≥85% on merged HF + GGUF) → GGUF convert + LM Studio install →
chat-loop verification on `llama-server`.

Full reference: `docs/fine_tune/WIZARD.md`.

## Security model

Auth and isolation are configured via `.env` and `app/config.py`. Defaults
err on the safe side; production install (this machine) has all of these
on.

- **Bearer token auth** — `REQUIRE_AUTH=true` enables `AuthMiddleware` on
  every endpoint. Tokens are generated by `python -m app.cli setup` and
  scoped per-agent (`anvil`, `claude`, `codex`, `gemini`, `python-httpx`).
- **Trusted-agent bypass** — `TRUSTED_AGENTS` allows a known agent name
  via the `X-Agent-Name` header on localhost only. Hooks use this so the
  recorder never has to ship a token to `~/.claude/`.
- **Host bound to `127.0.0.1`** — no external interface ever.
- **CORS** locked to localhost origins.
- **Rate limits** — `100/min` writes, `500/min` reads (`RateLimitMiddleware`).
- **Secret redaction** — `REDACT_SECRETS=true` by default. Strips API
  keys, tokens, and password-shaped strings from `tool_input` before
  persistence. See `app/redact.py`.
- **Audit logging** — `audit_log_level=writes_only`, 30-day retention.
  See `AuditMiddleware`.
- **PG trust-auth warning** — startup logs CRITICAL if `POSTGRES_PASSWORD`
  is empty and `ALLOW_TRUST_AUTH=true` is not explicitly set.

There is no `docs/security.md` yet — read `app/middleware.py`, `app/auth.py`,
`app/redact.py`, and the security block in `app/config.py` for current
behavior.

## Daily backups

`launchd` runs `pg_dump` at **03:14 local time** and writes
`data/backups/daily_YYYYMMDD_HHMMSS.sql.gz`. Retention keeps the **3
most recent** `daily_*.sql.gz`; manually-created snapshots
(`pre_v2_backfill_*.sql.gz`, etc.) are never auto-deleted.

```bash
# Install (idempotent — safe to re-run, called from session-start hook too)
bash scripts/install_backup_schedule.sh

# Verify
bash scripts/install_backup_schedule.sh --check
ls -lht data/backups/daily_*.sql.gz | head -3

# Manual snapshot
bash scripts/backup.sh
```

The plist installed is `~/Library/LaunchAgents/com.metazen.agent-memory-backup.plist`.
Operator details: `docs/backups.md`.

## Where to read next

- `HANDOFF.md` — current state (v2 retraction status, v3 plan refs, setup-on-new-machine, resume commands)
- `AGENTS.md` — file-map operating guide for agents working in this repo
- `docs/fine_tune/V3_PLAN.md` — current training plan with multi-turn fixes
- `docs/fine_tune/FAILURE_MODES.md` — 12 known failure modes + fixes (start here when something breaks)
- `docs/fine_tune/WIZARD.md` — wizard reference
- `docs/fine_tune/PIPELINE_RUNBOOK.md` — phase-by-phase training recipe
- `docs/fine_tune/V2_DATA_PIPELINE_PLAN.md` — full data-pipeline design (prompt↔tool_call linkage, project consolidation, build_v2_dataset)
- `docs/training_runs/` — per-run reports including the v2 real-world A/B
- `docs/backups.md` — daily backup operator reference
- `docs/PRIMER.md` — multi-agent integration guide (Cursor, Windsurf, Cline, Codex, Zed, custom)

## License + contact

No `LICENSE` file is committed. Treat the repo as private until one is added.
Maintainer: `mz@wfca.com` (see `~/.claude/CLAUDE.md`).
