# agent-memory

Persistent cross-session memory for AI coding agents. Records what was learned, built, fixed, and decided during each session, then makes it searchable via semantic + full-text hybrid search.

Built as a standalone replacement for the `claude-mem` plugin (which crashed due to ChromaDB/Rust segfaults on macOS ARM64).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code Session                                    │
│                                                         │
│  session-start hook ──► GET context + inject hints      │
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
│  PostgreSQL 16 + pgvector (Docker, port 5433)           │
│  Database: agentic  │  User: wfhub                      │
│  Tables: mem_* prefixed (avoids collisions)             │
└─────────────────────────────────────────────────────────┘
```

## Database Schema

All tables use the `mem_` prefix to coexist with other services in the shared `agentic` database.

### Tables

| Table | Purpose |
|-------|---------|
| `embedding_models` | Registry of embedding models (supports model switching) |
| `mem_projects` | Auto-created from working directory basename |
| `mem_sessions` | One per Claude Code session (active/completed/failed) |
| `mem_observations` | Core memory unit — structured LLM-extracted knowledge |
| `mem_observation_queue` | Async processing queue (never blocks hooks) |
| `mem_user_prompts` | Optional user prompt timeline |

### mem_observations (core table)

| Column | Type | Description |
|--------|------|-------------|
| `id` | `SERIAL` | Primary key |
| `session_id` | `INTEGER` | FK to `mem_sessions` |
| `project_id` | `INTEGER` | FK to `mem_projects` |
| `title` | `TEXT` | Short descriptive title |
| `subtitle` | `TEXT` | One-line context |
| `type` | `TEXT` | `decision\|bugfix\|feature\|refactor\|discovery\|change\|pattern\|gotcha` |
| `narrative` | `TEXT` | 2-3 sentence description |
| `facts` | `JSONB` | Extracted facts array |
| `concepts` | `JSONB` | Concept tags array |
| `files_read` | `JSONB` | Files that were read |
| `files_modified` | `JSONB` | Files that were modified |
| `raw_text` | `TEXT` | Combined text for re-embedding (never lose this) |
| `embedding` | `vector(768)` | pgvector embedding |
| `embedding_model_id` | `INTEGER` | FK to `embedding_models` |
| `tool_name` | `TEXT` | Source tool that triggered this observation |
| `created_at` | `TIMESTAMPTZ` | When the observation was created |
| `tsv` | `tsvector` | Auto-generated full-text search column (weighted A/B/C/D) |

### Indexes

- `idx_mem_obs_project` — project_id
- `idx_mem_obs_type` — observation type
- `idx_mem_obs_created` — created_at DESC
- `idx_mem_obs_tsv` — GIN index on tsvector
- `idx_mem_obs_embedding` — HNSW index (m=16, ef_construction=64) for cosine similarity

### Search Strategy

Hybrid search using **Reciprocal Rank Fusion (RRF)** with k=60:
1. **Vector search** — cosine similarity via pgvector HNSW index
2. **Full-text search** — PostgreSQL tsvector with weighted fields (title=A, subtitle=B, narrative=C, raw_text=D)
3. **RRF fusion** — `score = sum(1/(60+rank))` across both result sets

## Components

### FastAPI Server (`app/`)

The HTTP API for hooks and direct access.

| File | Purpose |
|------|---------|
| `main.py` | App lifecycle (pool init, schema migration, queue worker) |
| `config.py` | Pydantic settings from `.env` |
| `db.py` | asyncpg connection pool |
| `models.py` | Pydantic schemas (QueueItem, Observation, Session, Search) |
| `embeddings.py` | sentence-transformers in-process embeddings (768-dim) |
| `observation_llm.py` | Local GGUF (Qwen2.5-1.5B) with Anthropic Haiku fallback |
| `queue_worker.py` | Background asyncio task, processes queue items |
| `routes/health.py` | `GET /api/health` — DB, embeddings, queue status |
| `routes/observations.py` | Queue ingest, CRUD, hybrid search |
| `routes/sessions.py` | Session lifecycle (create, update, list) |
| `routes/admin.py` | Stats, background re-embed job |

### MCP Server (`mcp_server.py`)

Self-contained stdio MCP server for Claude Code. Has its own DB pool and embedding model — zero dependency on the FastAPI server.

**Tools:**

| Tool | Description |
|------|-------------|
| `search` | Semantic + FTS hybrid search. Returns index with IDs. |
| `timeline` | Get observations around an anchor (by ID or query) |
| `get_observations` | Fetch full details for specific IDs |
| `save_memory` | Manually save a memory/observation |

**3-layer workflow** (optimized for token savings):
1. `search(query)` — get index with IDs (~50-100 tokens/result)
2. `timeline(anchor=ID)` — get context around interesting results
3. `get_observations([IDs])` — fetch full details only for filtered IDs

### Observation LLM (`app/observation_llm.py`)

Extracts structured observations from tool calls. Two-tier strategy:

1. **Primary**: Local GGUF model via `llama-cpp-python` — Qwen2.5-1.5B-Instruct (Q4_K_M, ~1GB RAM)
2. **Fallback**: Anthropic Claude Haiku (if `ANTHROPIC_API_KEY` is set)

Low-value tools are skipped automatically (task management, plan mode, skill invocations).

### Embeddings (`app/embeddings.py`)

In-process embeddings via `sentence-transformers`:
- **Model**: `nomic-ai/nomic-embed-text-v1.5` (768 dimensions)
- Runs in thread pool to avoid blocking the event loop
- Supports batch embedding for re-embed operations
- No external service dependency (no Ollama needed)

### Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `init_db.sql` | Schema DDL (idempotent, runs on server startup) |
| `migrate_claude_mem.py` | One-time migration from claude-mem SQLite DB |
| `re_embed.py` | Standalone re-embed script (cursor-based pagination) |

## API Endpoints

### Health & Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | DB, embeddings, queue depth |
| `GET` | `/api/admin/stats` | Observation/session/project counts, type breakdown |
| `POST` | `/api/admin/re-embed` | Start background re-embed job (`?only_missing=true`) |
| `GET` | `/api/admin/re-embed/status` | Check re-embed progress |
| `POST` | `/api/admin/re-embed/cancel` | Cancel running re-embed |

### Observations

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/queue` | Queue tool call for async observation extraction |
| `POST` | `/api/observations` | Create observation directly (bypasses queue) |
| `GET` | `/api/observations` | List observations (`?project=&type=&limit=&offset=`) |
| `GET` | `/api/observations/{id}` | Get single observation |
| `POST` | `/api/observations/search` | Hybrid search (body: `{query, project, type[], limit, mode}`) |

### Sessions

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/sessions` | Start new session |
| `PATCH` | `/api/sessions/{id}` | Update session (status, summary) |
| `GET` | `/api/sessions` | List sessions (`?project=&status=&limit=`) |

## Setup

### Prerequisites

- Python 3.12+ (3.13 has ChromaDB issues — avoid if migrating from claude-mem)
- Docker (for PostgreSQL + pgvector)

### 1. Start the database

```bash
cd docker
docker compose up -d
```

This starts PostgreSQL 16 with pgvector on port 5433. The `init_db.sql` schema runs automatically on first start via Docker's entrypoint.

### 2. Create Python environment

```bash
cd /path/to/agentMemory
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — the defaults work for local Docker setup
```

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://wfhub:@localhost:5433/agentic` | PostgreSQL connection |
| `EMBEDDING_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | sentence-transformers model |
| `OBSERVATION_LLM_MODEL` | *(path to .gguf)* | Local LLM for observation extraction |
| `ANTHROPIC_API_KEY` | *(empty)* | Optional: Haiku fallback for observations |
| `PORT` | `3377` | FastAPI server port |

### 4. Start the server

```bash
source .venv/bin/activate
uvicorn app.main:app --port 3377
```

The server will:
- Initialize the asyncpg connection pool
- Run `init_db.sql` (idempotent schema migration)
- Start the background queue worker
- Load the embedding model on first use (lazy)

### 5. Register MCP server

Add to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "agent-memory": {
      "type": "stdio",
      "command": "/path/to/agentMemory/.venv/bin/python",
      "args": ["/path/to/agentMemory/mcp_server.py"]
    }
  }
}
```

### 6. Install hooks

The Claude Code hooks live in the companion repo at `hooks/agent-memory/`. See that repo's README for hook installation.

## Migration from claude-mem

One-time migration from the claude-mem SQLite database:

```bash
source .venv/bin/activate

# Dry run — see what would be migrated
python scripts/migrate_claude_mem.py --dry-run

# Migrate without embeddings (fast, re-embed later)
python scripts/migrate_claude_mem.py

# Migrate with embeddings (slow but complete)
python scripts/migrate_claude_mem.py --embed --batch-size 50
```

Re-embed existing observations after migration:

```bash
# Only observations missing embeddings
python scripts/re_embed.py --only-missing

# Re-embed everything (e.g. after model change)
python scripts/re_embed.py --batch-size 100
```

Or via the API while the server is running:

```bash
curl -X POST http://localhost:3377/api/admin/re-embed?only_missing=true
curl http://localhost:3377/api/admin/re-embed/status
```

## Data Flow

### Recording (write path)

```
Tool executes in Claude Code
    │
    ▼
post-tool-use hook fires (5s timeout, fire-and-forget)
    │
    ▼
POST /api/queue  ──►  mem_observation_queue (status=pending)
    │
    ▼
Queue Worker dequeues (FOR UPDATE SKIP LOCKED)
    │
    ▼
Observation LLM extracts structured data (title, type, narrative, facts...)
    │
    ▼
sentence-transformers generates 768-dim embedding
    │
    ▼
INSERT into mem_observations with embedding vector
```

### Retrieval (read path)

```
Claude Code invokes MCP tool (search/timeline/get_observations)
    │
    ▼
MCP server embeds query via sentence-transformers
    │
    ▼
Parallel: pgvector cosine search + PostgreSQL FTS
    │
    ▼
RRF fusion ranks results
    │
    ▼
Returns JSON to Claude Code
```

## Docker

The `docker/` directory contains a self-contained compose file for the database:

```bash
# Start
cd docker && docker compose up -d

# Stop
cd docker && docker compose down

# Reset (destroys data)
cd docker && docker compose down -v
```

**Image**: `pgvector/pgvector:pg16` — PostgreSQL 16 with the pgvector extension pre-installed.

The compose file mounts `scripts/init_db.sql` into `/docker-entrypoint-initdb.d/` so the schema is created automatically on first run.

**Note**: If you're already running the `wfhub-v2` Docker stack (agentmz project), the database is shared — both use `wfhub` user on port 5433, database `agentic`. You only need one running.
