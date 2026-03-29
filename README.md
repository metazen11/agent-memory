# agent-memory

Persistent memory for AI coding agents that build, maintain, and enhance long-lived projects.

Most memory solutions assume your relationship with a project ends at `git push`. This one doesn't. If you maintain production systems, ship continuous improvements, and need your agent to remember why that Docker port was changed 4 months ago — agent-memory is built for you.

Records what was learned, built, fixed, and decided during each session, then makes it searchable via semantic + full-text hybrid search. Claude Code's built-in `MEMORY.md` gives you 200 lines of pinned notes. agent-memory gives you a searchable journal across thousands of observations — so accumulated context becomes a competitive advantage, not a truncated file.

Works with Claude Code out of the box. Designed to support any AI coding agent via REST API or MCP.

## Quick Start

```bash
git clone https://github.com/metazen11/agent-memory.git
cd agent-memory
node install.js
```

The installer handles everything:
- Creates Python venv and installs dependencies
- Downloads embedding model (~400MB) and observation LLM (~1GB)
- Generates `.env` with random Postgres password
- Starts PostgreSQL (native Homebrew preferred, Docker fallback)
- Starts FastAPI server on port 3377
- Registers MCP server, hooks, and skills in Claude Code

### Commands

```bash
node install.js              # Full setup + install
node install.js --status     # Show what's installed and running
node install.js --start      # Start services (Docker + FastAPI)
node install.js --stop       # Stop services
node install.js --migrate    # Run pending database migrations
node install.js --migrate --dry-run  # Preview migrations (no changes)
node install.js --migrate --backup   # Backup tables, then migrate
node install.js --backup     # Backup mem_* tables only
node install.js --uninstall  # Remove hooks, MCP, skills
```

### Prerequisites

- **PostgreSQL 16 + pgvector** — macOS (recommended): `brew install postgresql@16 pgvector` | Docker fallback: `brew install --cask docker` | Linux: `sudo apt install docker.io docker-compose-plugin`
- **Python 3.12+** — macOS: `brew install python@3.12` | Linux: `sudo apt install python3.12 python3.12-venv`
- **Node.js** — for the installer and hooks

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code Session                                    │
│                                                         │
│  session-start hook ──► Health check → auto-start       │
│                     └──► Inject MCP guide + context     │
│  post-tool-use hook ──► POST /api/queue (fire & forget) │
│  session-end hook   ──► PATCH /api/sessions/:id         │
└──────────────┬──────────────────────────────────────────┘
               │ HTTP (localhost:3377)
┌──────────────▼──────────────────────────────────────────┐
│  FastAPI Server (uvicorn, port 3377)                    │
│                                                         │
│  /api/queue ──► observation_queue table                  │
│  /api/observations ──► CRUD + hybrid search             │
│  /api/sessions ──► session lifecycle                     │
│  /api/admin ──► stats, re-embed                         │
│                                                         │
│  Queue Worker (background asyncio task)                 │
│  ├─ Dequeue pending items (FOR UPDATE SKIP LOCKED)      │
│  ├─ Generate observation via LLM (local GGUF → Haiku)   │
│  ├─ Embed via sentence-transformers (in-process)        │
│  └─ Insert into mem_observations with vector            │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│  MCP Server (stdio, separate process)                   │
│  Registered in ~/.claude/.mcp.json                      │
│                                                         │
│  Tools: search, timeline, get_observations, save_memory │
│  Own DB pool + embedding model (zero FastAPI deps)      │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│  PostgreSQL 16 + pgvector                               │
│  Native (Homebrew, launchd) or Docker container         │
│  Tables: mem_* prefixed (avoids collisions)             │
└─────────────────────────────────────────────────────────┘
```

## How It Works

### Recording (write path)

Every tool call in your coding session is captured:

1. **PostToolUse hook** fires (fire-and-forget, ~40ms)
2. Tool call data queued to `/api/queue`
3. Background worker dequeues with `FOR UPDATE SKIP LOCKED`
4. Local LLM extracts structured observation (title, type, narrative, facts)
5. Sentence-transformers generates 768-dim embedding
6. Inserted into PostgreSQL with pgvector index

### Retrieval (read path)

Search past sessions via MCP tools (3-layer workflow):

1. `search(query)` — hybrid vector + full-text search, returns IDs (~50-100 tokens/result)
2. `timeline(anchor=ID)` — context around interesting results
3. `get_observations([IDs])` — full details only for filtered IDs

Never skip to step 3. Always filter first. 10x token savings.

### Auto-start

The session-start hook automatically starts services if they're not running. No manual intervention needed after initial install.

## Configuration

### .env

Generated by `install.js`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_USER` | `agentmem` | PostgreSQL user |
| `POSTGRES_PASSWORD` | *(generated)* | PostgreSQL password |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `agent_memory` | Database name |
| `DATABASE_URL` | *(built from above)* | Full URL override |
| `EMBEDDING_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | Sentence-transformers model |
| `OBSERVATION_LLM_MODEL` | *(path to .gguf)* | Local LLM for observation extraction |
| `ANTHROPIC_API_KEY` | *(empty)* | Haiku fallback if no local LLM |
| `PORT` | `3377` | FastAPI server port |

### Existing Database (Bring Your Own Postgres)

If you already have a PostgreSQL 16+ instance with pgvector, set `DATABASE_URL` in `.env`:

```bash
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

When `DATABASE_URL` is set, the installer:
- Skips Docker entirely (no container needed)
- Runs versioned SQL migrations against your database
- Creates all `mem_`-prefixed tables (avoids collisions with other apps)

Requirements for external databases:
- PostgreSQL 16+ with the `vector` extension (pgvector)
- A database and user with CREATE TABLE / CREATE EXTENSION permissions

### Schema Migrations

The database schema is managed by versioned SQL migrations in `scripts/migrations/`:

```
scripts/migrations/
├── 001-initial-schema.sql     # Tables, indexes, pgvector extension
├── 002-add-new-feature.sql    # Future migrations...
└── ...
```

Migrations run automatically:
- During `node install.js` (step 7)
- On every FastAPI server startup
- Via `python scripts/run_migrations.py` (manual)

Each migration runs exactly once. A `mem_schema_migrations` table tracks which have been applied.

## Components

### FastAPI Server (`app/`)

| File | Purpose |
|------|---------|
| `main.py` | App lifecycle (pool init, migrations, queue worker) |
| `migrate.py` | Versioned SQL migration runner |
| `config.py` | Pydantic settings from `.env` |
| `db.py` | asyncpg connection pool |
| `models.py` | Pydantic schemas |
| `embeddings.py` | Sentence-transformers in-process embeddings (768-dim) |
| `observation_llm.py` | Local GGUF (Qwen2.5-1.5B) with Anthropic Haiku fallback |
| `queue_worker.py` | Background asyncio task, processes queue items |
| `routes/` | Health, observations, sessions, admin endpoints |

### MCP Server (`mcp_server.py`)

Self-contained stdio MCP server. Own DB pool and embedding model — zero dependency on FastAPI.

### Hooks (`hooks/`)

| Hook | Event | Timeout | Description |
|------|-------|---------|-------------|
| `session-start.js` | SessionStart | 60s | Health check, auto-start services, inject context |
| `post-tool-use.js` | PostToolUse | 5s | Fire-and-forget observation capture |
| `session-end.js` | Stop | 10s | Mark session completed |
| `ensure-services.js` | *(internal)* | — | Starts Docker + FastAPI when called by session-start |

### Skills (`skills/`)

`/mem-search` — User-invocable skill for searching past sessions.

## API Endpoints

### Health & Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | DB, embeddings, queue depth |
| `GET` | `/api/admin/stats` | Counts and type breakdown |
| `POST` | `/api/admin/re-embed` | Background re-embed job |

### Observations

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/queue` | Queue tool call for async extraction |
| `POST` | `/api/observations` | Create observation directly |
| `GET` | `/api/observations` | List with filters |
| `POST` | `/api/observations/search` | Hybrid search |

### Sessions

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/sessions` | Start new session |
| `PATCH` | `/api/sessions/{id}` | Update session status |
| `GET` | `/api/sessions` | List sessions |

## Database Schema

All tables use the `mem_` prefix.

| Table | Purpose |
|-------|---------|
| `embedding_models` | Registry of embedding models |
| `mem_projects` | Auto-created from working directory |
| `mem_sessions` | One per coding session |
| `mem_observations` | Core memory unit with embeddings |
| `mem_observation_queue` | Async processing queue |
| `backfill_log` | JSONL backfill progress tracking (per-session) |

### Search Strategy

Hybrid search using **Reciprocal Rank Fusion (RRF)** with k=60:
1. **Vector search** — cosine similarity via pgvector HNSW index
2. **Full-text search** — PostgreSQL tsvector with weighted fields
3. **RRF fusion** — `score = sum(1/(60+rank))` across both result sets

## Multi-Agent Support

The system is agent-agnostic. The hooks are the Claude-specific integration layer.

**REST API** — Any agent can POST to `/api/queue` and GET from `/api/observations`.

**MCP** — Register `mcp_server.py` in any MCP-compatible agent's config.

**Direct SQL** — Query `mem_observations` with pgvector operators.

See **[docs/PRIMER.md](docs/PRIMER.md)** for the full multi-agent integration guide with config snippets for Claude Code, Cursor, Windsurf, Cline, Codex CLI, Zed, VS Code Copilot, and custom agents.

## Why Replace claude-mem?

This project was built as a direct replacement for [claude-mem](https://github.com/thedotmack/claude-mem) after hitting persistent stability issues:

- **PostToolUse hook hangs** — claude-mem's `PostToolUse` hook uses `matcher: "*"` with a 120-second timeout. It fires on every single tool call, spawns worker-service daemons, and frequently hangs waiting for ChromaDB sync. This blocks Claude Code after every tool use. The fix (removing the hook from `hooks.json`) gets overwritten on every plugin update.
- **Zombie processes** — The worker-service daemons accumulate. We've seen 50-80+ zombie `worker-service` processes in a single session, consuming memory and CPU.
- **ChromaDB crashes on Apple Silicon** — ChromaDB 1.5.0's Rust bindings (`chromadb_rust_bindings.abi3.so`) segfault on macOS ARM64 due to a thread-safety bug. Multiple tokio workers contend on a mutex, causing SIGSEGV.
- **No real vector search** — claude-mem uses ChromaDB/SQLite locally, which doesn't scale well and lacks proper hybrid search. agent-memory uses PostgreSQL + pgvector with HNSW indexes and Reciprocal Rank Fusion (vector + full-text).
- **No auto-recovery** — When claude-mem's database or services go down, they stay down. agent-memory's session-start hook auto-detects unhealthy services and restarts Docker containers and the FastAPI server automatically.
- **Fire-and-forget hooks** — agent-memory's PostToolUse hook writes stdout immediately and exits in ~30ms. The HTTP POST to the queue is unref'd so it never blocks the Node.js event loop. claude-mem's hook blocks until its worker completes.

If you're currently using claude-mem and experiencing hangs, crashes, or zombie processes, agent-memory is a drop-in replacement with a migration script included.

## Backfill from JSONL Logs

Recover observations from Claude Code's JSONL session logs (e.g. after data loss or for historical backfill). Uses the same LLM + embedding pipeline as the live queue worker.

```bash
# Preview what would be processed
.venv/bin/python scripts/backfill_jsonl.py --dry-run

# Backfill all sessions from default Claude projects dir
.venv/bin/python scripts/backfill_jsonl.py

# Scope to a specific project's logs
.venv/bin/python scripts/backfill_jsonl.py --jsonl-dir ~/.claude/projects/-Users-mz-Dropbox--CODING-myproject

# Process a single session
.venv/bin/python scripts/backfill_jsonl.py --session 08d5d131-2765-4f1e-bc38-276f237f9d4d

# Re-scan completed sessions for specific tool types (e.g. after unblocking a tool)
.venv/bin/python scripts/backfill_jsonl.py --reprocess-tools AskUserQuestion ExitPlanMode
```

**Resume support:** Progress is tracked per-session in the `backfill_log` table. If interrupted (Ctrl-C, Docker crash, etc.), re-running the same command skips completed sessions and resumes in-progress ones from the last checkpoint.

**Performance:** ~10s per tool call (local GGUF LLM inference). A 500-tool-call backfill takes ~1.5 hours.

## Backup

```bash
bash scripts/backup.sh                # Manual backup (pg_dump, gzipped)
```

Backups go to `data/backups/` (Dropbox-synced). Retains the last 3 daily backups. Install as a cron job for automated daily backups:

```bash
crontab -e
# Add: 0 3 * * * /path/to/agentMemory/scripts/backup.sh
```

## Migration from claude-mem

```bash
source .venv/bin/activate
python scripts/migrate_claude_mem.py       # migrate without embeddings
python scripts/migrate_claude_mem.py --embed  # migrate with embeddings
python scripts/re_embed.py --only-missing  # embed missing observations
```

## Debug

| Hook | Default | Toggle |
|------|---------|--------|
| session-start | ON | `AGENT_MEMORY_DEBUG=0` |
| post-tool-use | OFF | `AGENT_MEMORY_DEBUG=1` |
| session-end | ON | `AGENT_MEMORY_DEBUG=0` |

```bash
AGENT_MEMORY_DEBUG=1 claude   # enable all
```

## Native PostgreSQL (recommended on macOS)

Native Homebrew Postgres runs as a launchd service — auto-starts on boot, auto-restarts on crash, no Docker VM overhead. The installer and hooks detect native postgres automatically and prefer it over Docker.

### Fresh install

```bash
brew install postgresql@16 pgvector
brew services start postgresql@16
node install.js
```

### Migrate from Docker

```bash
bash scripts/migrate_to_native_pg.sh          # idempotent — safe to re-run
bash scripts/migrate_to_native_pg.sh --dry-run # preview what would happen
```

The migration script:
1. Installs postgresql@16 + pgvector via Homebrew (if needed)
2. Starts the service (if not running)
3. Creates role and database (if not exists)
4. Enables pgvector extension
5. Imports data from Docker container (if running and native is empty)
6. Runs schema migrations
7. Updates `.env` with native DATABASE_URL

After migration, Docker Desktop can be stopped entirely.

## Docker (fallback)

```bash
cd docker && docker compose up -d     # start
cd docker && docker compose down      # stop
cd docker && docker compose down -v   # reset (destroys data)
```

Docker is used automatically on Linux or when native Homebrew postgres is not available.
