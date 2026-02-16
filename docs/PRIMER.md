# Agent Memory: Multi-Agent Integration Guide

Persistent cross-session memory for AI coding agents. Records what was learned, built, fixed, and decided during each session, then makes it searchable via semantic + full-text hybrid search. The server is agent-agnostic — any agent can integrate via REST API, MCP, or both.

## Three Integration Layers

| Layer | Protocol | Agents | Effort |
|-------|----------|--------|--------|
| **REST API** | HTTP POST/GET | Any agent, any language | Lowest — just HTTP calls |
| **MCP Server** | stdio (Model Context Protocol) | Claude Code, Cursor, Windsurf, Cline, Codex CLI, Zed, VS Code Copilot, any MCP-compatible | Medium — config file only |
| **Hooks** | Lifecycle scripts | Claude Code (built-in), others via adapter | Highest — write scripts per agent |

Most agents should start with the **REST API** (works everywhere) and add **MCP** if their platform supports it. Hooks are optional automation — they capture tool calls and manage session lifecycle automatically.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Your AI Agent (Claude, Cursor, Windsurf, Aider, etc.)  │
│                                                         │
│  Option A: MCP tools (search, timeline, save_memory)    │
│  Option B: REST API calls (curl / fetch / requests)     │
│  Option C: Hooks (auto-capture tool calls)              │
└──────────────┬──────────────────────────────────────────┘
               │ HTTP (localhost:3377) or stdio (MCP)
┌──────────────▼──────────────────────────────────────────┐
│  FastAPI Server (uvicorn, port 3377)                    │
│                                                         │
│  /api/queue ──► observation_queue table                  │
│  /api/observations ──► CRUD + hybrid search             │
│  /api/sessions ──► session lifecycle                     │
│  /api/health ──► health check                            │
│                                                         │
│  Queue Worker (background asyncio task)                 │
│  ├─ Dequeue pending items (FOR UPDATE SKIP LOCKED)      │
│  ├─ Generate observation via LLM (local GGUF → Haiku)   │
│  ├─ Embed via sentence-transformers (768-dim, in-proc)  │
│  └─ Insert into mem_observations with pgvector          │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│  MCP Server (stdio, separate process)                   │
│  Tools: search, timeline, get_observations, save_memory │
│  Own DB pool + embedding model (zero FastAPI dependency) │
└──────────────┬──────────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────────┐
│  PostgreSQL 16 + pgvector (Docker or external)          │
│  Tables: mem_* prefixed (avoids collisions)             │
└─────────────────────────────────────────────────────────┘
```

---

## 1. Server Setup (shared by all agents)

### Prerequisites

| Dependency | macOS | Linux |
|------------|-------|-------|
| Docker *(or external Postgres)* | `brew install --cask docker` | `sudo apt install docker.io docker-compose-plugin` |
| Python 3.12+ | `brew install python@3.12` | `sudo apt install python3.12 python3.12-venv` |
| Node.js 18+ | `brew install node` | `sudo apt install nodejs` |

### Install

```bash
git clone https://github.com/metazen11/agent-memory.git
cd agent-memory
node install.js
```

The installer runs 11 steps:
1. Check prerequisites (Docker, Python, Node)
2. Create Python venv (`.venv/`)
3. Install dependencies (FastAPI, asyncpg, sentence-transformers)
4. Download embedding model (~400MB: nomic-ai/nomic-embed-text-v1.5)
5. Download observation LLM (~1GB: Qwen2.5-1.5B-Instruct GGUF)
6. Generate `.env` with random Postgres password
7. Start Docker (PostgreSQL 16 + pgvector on port 5433)
8. Run schema migrations
9. Start FastAPI server on port 3377
10. Register MCP server in Claude Code *(skip for non-Claude agents)*
11. Install hooks in Claude Code *(skip for non-Claude agents)*

### Verify

```bash
curl -s localhost:3377/api/health | python3 -m json.tool
```

Expected response:

```json
{
  "db": { "status": "ok", "version": "PostgreSQL 16.x", "pgvector": true },
  "embeddings": { "status": "ok" },
  "queue": { "pending": 0, "observations_total": 0 },
  "status": "ok"
}
```

### Bring Your Own Postgres (BYOP)

If you already have PostgreSQL 16+ with pgvector, set `DATABASE_URL` in `.env`:

```bash
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

When set, the installer skips Docker entirely. Requirements:
- PostgreSQL 16+ with `vector` extension installed
- A user with CREATE TABLE / CREATE EXTENSION permissions
- All tables use the `mem_` prefix (safe for shared databases)

### Service Management

```bash
node install.js --status     # Check what's running
node install.js --start      # Start Docker + FastAPI
node install.js --stop       # Stop services
node install.js --migrate    # Run pending migrations
node install.js --backup     # Backup mem_* tables
```

---

## 2. Integration Layer 1: REST API (universal)

Works with any agent that can make HTTP requests. No SDK, no protocol — just `curl`, `fetch`, `requests`, or equivalent.

### 2.1 Session Lifecycle

#### Start a session

```bash
curl -X POST localhost:3377/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "my-agent-session-001",
    "project": "my-project",
    "project_path": "/path/to/my-project",
    "agent_type": "cursor"
  }'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Unique ID for this session (use UUID or timestamp) |
| `project` | string | yes | Project name (usually the directory basename) |
| `project_path` | string | no | Full filesystem path |
| `agent_type` | string | no | Agent identifier (default: `"claude-code"`) |

Returns `201` with session details. Returns `409` if `session_id` already exists.

#### End a session

```bash
curl -X PATCH localhost:3377/api/sessions/my-agent-session-001 \
  -H 'Content-Type: application/json' \
  -d '{"status": "completed"}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `status` | string | no | `"completed"`, `"active"`, `"failed"` |
| `summary` | string | no | Human-readable session summary |

#### List sessions

```bash
curl 'localhost:3377/api/sessions?project=my-project&status=completed&limit=10'
```

### 2.2 Recording Observations (write path)

Two approaches: **queue** (async, recommended) or **direct** (synchronous).

#### Option A: Queue (recommended)

Fire-and-forget. The server's background worker extracts a structured observation via LLM, generates an embedding, and stores it. Best for capturing tool calls in bulk.

```bash
curl -X POST localhost:3377/api/queue \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "my-agent-session-001",
    "tool_name": "file_edit",
    "tool_input": {"file": "src/auth.py", "action": "replace"},
    "tool_response_preview": "File updated successfully. Changed login() to use JWT tokens instead of session cookies.",
    "cwd": "/path/to/my-project",
    "last_user_message": "Switch auth from sessions to JWT"
  }'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Must match an existing session (or auto-created) |
| `tool_name` | string | no | Name of the tool/action (e.g. `"file_edit"`, `"bash"`, `"search"`) |
| `tool_input` | object | no | Tool parameters/arguments |
| `tool_response_preview` | string | no | First ~2000 chars of tool output |
| `cwd` | string | no | Working directory (used to derive project name) |
| `last_user_message` | string | no | User's prompt that triggered this action |

Returns `{"status": "queued"}`. The background worker processes it asynchronously.

#### Option B: Direct observation creation

Create a structured observation immediately. You extract the structure yourself.

```bash
curl -X POST localhost:3377/api/observations \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "my-agent-session-001",
    "project": "my-project",
    "title": "Switched authentication from sessions to JWT",
    "type": "feature",
    "narrative": "Replaced Flask-Login session cookies with PyJWT tokens. Added token refresh endpoint at /api/auth/refresh. Tokens expire after 24 hours.",
    "facts": ["JWT tokens replace session cookies", "Token expiry set to 24h", "Refresh endpoint at /api/auth/refresh"],
    "concepts": ["what-changed", "how-it-works"],
    "files_read": ["src/auth.py", "src/config.py"],
    "files_modified": ["src/auth.py", "src/routes/auth.py"]
  }'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `session_id` | string | yes | — | Session identifier |
| `project` | string | yes | — | Project name |
| `title` | string | yes | — | What happened (1 line) |
| `subtitle` | string | no | null | Additional context |
| `type` | string | no | `"discovery"` | Observation type (see reference below) |
| `narrative` | string | no | null | Detailed description |
| `facts` | string[] | no | `[]` | Concrete facts extracted |
| `concepts` | string[] | no | `[]` | Conceptual tags |
| `files_read` | string[] | no | `[]` | Files that were read |
| `files_modified` | string[] | no | `[]` | Files that were changed |
| `tool_name` | string | no | null | Tool that produced this |
| `prompt_number` | int | no | null | Which prompt in the session |

### 2.3 Searching Memories (read path)

#### Hybrid search (recommended)

Combines vector similarity and PostgreSQL full-text search using Reciprocal Rank Fusion.

```bash
curl -X POST localhost:3377/api/observations/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "JWT authentication implementation",
    "project": "my-project",
    "limit": 10
  }'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | yes | — | Natural language search query |
| `project` | string | no | null | Filter by project name |
| `cross_project` | bool | no | `false` | Search across all projects |
| `type` | string[] | no | null | Filter by type(s): `["bugfix", "feature"]` |
| `limit` | int | no | `10` | Max results (up to 100) |
| `mode` | string | no | `"hybrid"` | `"hybrid"`, `"vector"`, or `"fts"` |

Response:

```json
{
  "query": "JWT authentication implementation",
  "mode": "hybrid",
  "total": 3,
  "observations": [
    {
      "id": 42,
      "title": "Switched authentication from sessions to JWT",
      "type": "feature",
      "project_name": "my-project",
      "narrative": "Replaced Flask-Login session cookies with PyJWT tokens...",
      "facts": ["JWT tokens replace session cookies", "Token expiry set to 24h"],
      "score": 0.0331,
      "created_at": "2026-02-15T21:00:00"
    }
  ]
}
```

#### List with filters

```bash
# Recent discoveries in a project
curl 'localhost:3377/api/observations?project=my-project&type=discovery&limit=5'

# All bugfixes
curl 'localhost:3377/api/observations?type=bugfix&limit=20'
```

#### Get single observation

```bash
curl localhost:3377/api/observations/42
```

### 2.4 Health & Admin

```bash
# Health check
curl localhost:3377/api/health

# Stats overview (counts, type breakdown, project breakdown)
curl localhost:3377/api/admin/stats

# Trigger re-embedding of observations missing vectors
curl -X POST 'localhost:3377/api/admin/re-embed?only_missing=true'

# Check re-embed progress
curl localhost:3377/api/admin/re-embed/status
```

---

## 3. Integration Layer 2: MCP Server

The MCP server is a self-contained stdio process with its own database pool and embedding model. It does not depend on the FastAPI server — both can run independently.

### Register the MCP server

Add this to your agent's MCP configuration file. Replace `/absolute/path/to/agent-memory` with the actual install location.

```json
{
  "mcpServers": {
    "agent-memory": {
      "type": "stdio",
      "command": "/absolute/path/to/agent-memory/.venv/bin/python",
      "args": ["/absolute/path/to/agent-memory/mcp_server.py"]
    }
  }
}
```

### MCP config file locations

| Agent | Config File | Format |
|-------|-------------|--------|
| **Claude Code** | `~/.claude/.mcp.json` | `{ "mcpServers": { ... } }` |
| **Cursor** | `<project>/.cursor/mcp.json` or `~/.cursor/mcp.json` | `{ "mcpServers": { ... } }` |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | `{ "mcpServers": { ... } }` |
| **Cline** | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` (macOS) | `{ "mcpServers": { ... } }` |
| **Codex CLI** | `~/.codex/config.toml` | TOML `[mcp_servers.agent-memory]` |
| **VS Code Copilot** | `<project>/.vscode/mcp.json` | `{ "servers": { ... } }` |
| **Zed** | `~/.config/zed/settings.json` | JSON under `"context_servers"` |
| **Continue** | `~/.continue/config.json` | `{ "mcpServers": { ... } }` |

### MCP Tools Reference

The server exposes 5 tools:

#### `search` — Step 1: Get index with IDs

```json
{
  "query": "authentication bug",
  "project": "my-project",
  "type": "bugfix",
  "limit": 20,
  "dateStart": "2026-02-01",
  "dateEnd": "2026-02-15"
}
```

Returns lightweight results (~50-100 tokens each): `id`, `title`, `type`, `project`, `created_at`, `score`.

#### `timeline` — Step 2: Get context around results

```json
{
  "anchor": 42,
  "depth_before": 3,
  "depth_after": 3
}
```

Or find the anchor automatically:

```json
{
  "query": "authentication",
  "depth_before": 5,
  "depth_after": 5
}
```

Returns observations before and after the anchor in the same session.

#### `get_observations` — Step 3: Fetch full details

```json
{
  "ids": [42, 87, 103]
}
```

Returns complete observations (~500-1000 tokens each): title, narrative, facts, concepts, files.

#### `save_memory` — Store a manual observation

```json
{
  "text": "The auth system uses RS256 JWT with 24h expiry. Refresh tokens stored in HttpOnly cookies.",
  "title": "Auth Architecture Notes",
  "project": "my-project"
}
```

#### `memory_search_guide` — Usage reminder

No parameters. Returns the 3-layer workflow instructions.

### 3-Layer Search Workflow

**Always follow this order. Never skip to step 3.**

1. **`search(query)`** — Returns IDs + titles (~50-100 tokens/result)
2. **`timeline(anchor=ID)`** — Shows temporal context around interesting results
3. **`get_observations([IDs])`** — Fetches full details for selected IDs only

This saves ~10x tokens compared to fetching everything upfront.

---

## 4. Integration Layer 3: Hooks (lifecycle automation)

Hooks automate the write path — capturing tool calls and managing sessions without manual API calls. Claude Code has built-in support; other agents need adapters.

### Pattern A: Session Start

**Purpose**: Ensure services are running, inject recent memory into context.

**When**: Agent session begins.

**What to do**:
1. Health check: `GET localhost:3377/api/health`
2. If unhealthy: start services (Docker + FastAPI)
3. Register session: `POST /api/sessions`
4. Fetch recent context: `POST /api/observations/search` with project name
5. Inject results into system prompt or context

**Minimal example (bash)**:

```bash
#!/bin/bash
# On session start: register and fetch context
SESSION_ID="session-$(date +%s)"
PROJECT=$(basename "$PWD")

# Register session
curl -s -X POST localhost:3377/api/sessions \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\": \"$SESSION_ID\", \"project\": \"$PROJECT\", \"project_path\": \"$PWD\", \"agent_type\": \"my-agent\"}"

# Fetch recent memories for this project
curl -s -X POST localhost:3377/api/observations/search \
  -H 'Content-Type: application/json' \
  -d "{\"query\": \"recent work\", \"project\": \"$PROJECT\", \"limit\": 5}" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for obs in data.get('observations', []):
    print(f'- [{obs[\"type\"]}] {obs[\"title\"]}')
"
```

**Reference implementation**: `hooks/session-start.js` (Node.js, Claude Code specific)

### Pattern B: Tool Call Capture

**Purpose**: Record what the agent does for future recall.

**When**: After each tool call / action the agent takes.

**What to do**:
1. Capture: tool name, input, output preview, working directory
2. Fire-and-forget POST to `/api/queue`
3. Never block the agent — do this asynchronously

**Minimal example (Python)**:

```python
import requests
from threading import Thread

def capture_tool_call(session_id: str, tool_name: str, tool_input: dict,
                      tool_output: str, cwd: str):
    """Fire-and-forget: record tool call for async processing."""
    def _post():
        try:
            requests.post("http://localhost:3377/api/queue", json={
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_response_preview": tool_output[:2000],
                "cwd": cwd,
            }, timeout=2)
        except Exception:
            pass  # Never block the agent
    Thread(target=_post, daemon=True).start()
```

**What to skip**: Internal/meta tools that don't produce useful observations (e.g. list operations, plan mode toggles, task management). See skip list in `hooks/post-tool-use.js`.

**Reference implementation**: `hooks/post-tool-use.js` (Node.js, Claude Code specific)

### Pattern C: Session End

**Purpose**: Mark session completed for timeline tracking.

**When**: Agent session ends.

**What to do**: PATCH the session status.

**Minimal example**:

```bash
curl -X PATCH "localhost:3377/api/sessions/$SESSION_ID" \
  -H 'Content-Type: application/json' \
  -d '{"status": "completed", "summary": "Implemented JWT auth and wrote tests"}'
```

**Reference implementation**: `hooks/session-end.js` (Node.js, Claude Code specific)

---

## 5. Agent-Specific Quick Start

### Claude Code

Fully automated. The installer handles everything (MCP, hooks, skills):

```bash
git clone https://github.com/metazen11/agent-memory.git
cd agent-memory
node install.js
```

Done. Sessions auto-start, tool calls auto-capture, memory auto-searches via MCP tools and `/mem-search` skill.

### Cursor

MCP only (Cursor doesn't support lifecycle hooks). Create `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "agent-memory": {
      "type": "stdio",
      "command": "/absolute/path/to/agent-memory/.venv/bin/python",
      "args": ["/absolute/path/to/agent-memory/mcp_server.py"]
    }
  }
}
```

The agent can search and save memories via MCP tools. To record tool calls, manually POST to `/api/queue` (Cursor doesn't expose a hook mechanism for this).

### Windsurf

MCP supported. Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "agent-memory": {
      "type": "stdio",
      "command": "/absolute/path/to/agent-memory/.venv/bin/python",
      "args": ["/absolute/path/to/agent-memory/mcp_server.py"]
    }
  }
}
```

### Cline (VS Code Extension)

MCP supported with auto-approval. Add to your Cline MCP settings file:

**macOS**: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
**Linux**: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
**Windows**: `%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "/absolute/path/to/agent-memory/.venv/bin/python",
      "args": ["/absolute/path/to/agent-memory/mcp_server.py"],
      "alwaysAllow": ["search", "timeline", "get_observations", "save_memory", "memory_search_guide"]
    }
  }
}
```

The `alwaysAllow` field auto-approves memory tools so they don't interrupt your workflow.

### Codex CLI

MCP supported via TOML config. Add to `~/.codex/config.toml`:

```toml
[mcp_servers.agent-memory]
type = "stdio"
command = "/absolute/path/to/agent-memory/.venv/bin/python"
args = ["/absolute/path/to/agent-memory/mcp_server.py"]
```

### Zed

MCP supported via context servers. Add to `~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "agent-memory": {
      "command": {
        "path": "/absolute/path/to/agent-memory/.venv/bin/python",
        "args": ["/absolute/path/to/agent-memory/mcp_server.py"]
      }
    }
  }
}
```

### VS Code Copilot (1.96+)

Native MCP support. Create `<project>/.vscode/mcp.json`:

```json
{
  "servers": {
    "agent-memory": {
      "type": "stdio",
      "command": "/absolute/path/to/agent-memory/.venv/bin/python",
      "args": ["/absolute/path/to/agent-memory/mcp_server.py"]
    }
  }
}
```

Note: VS Code Copilot uses `"servers"` not `"mcpServers"`.

### Aider / CLI Tools

REST API only (no MCP support). Use wrapper scripts:

```bash
# Start session
aider_session_start() {
  export AGENT_MEMORY_SESSION="aider-$(date +%s)"
  curl -s -X POST localhost:3377/api/sessions \
    -H 'Content-Type: application/json' \
    -d "{\"session_id\": \"$AGENT_MEMORY_SESSION\", \"project\": \"$(basename $PWD)\", \"agent_type\": \"aider\"}" > /dev/null
}

# End session
aider_session_end() {
  curl -s -X PATCH "localhost:3377/api/sessions/$AGENT_MEMORY_SESSION" \
    -H 'Content-Type: application/json' \
    -d '{"status": "completed"}' > /dev/null
}

# Search memories before starting work
aider_recall() {
  curl -s -X POST localhost:3377/api/observations/search \
    -H 'Content-Type: application/json' \
    -d "{\"query\": \"$1\", \"project\": \"$(basename $PWD)\", \"limit\": 5}" \
    | python3 -m json.tool
}
```

### Custom Agents (pseudocode)

```
on_session_start():
    session_id = generate_uuid()
    POST /api/sessions {session_id, project, agent_type}
    recent = POST /api/observations/search {query: "recent work", project, limit: 5}
    inject recent.observations into system_prompt

on_tool_call(tool_name, input, output):
    # Fire and forget - never block
    async POST /api/queue {session_id, tool_name, tool_input: input, tool_response_preview: output[:2000], cwd}

on_session_end():
    PATCH /api/sessions/{session_id} {status: "completed"}

on_user_asks_about_past_work(query):
    results = POST /api/observations/search {query, project, limit: 10}
    if results need detail:
        full = GET /api/observations/{id}  # for each relevant ID
    return formatted results
```

---

## 6. System Prompt Injection Patterns

Memory is most useful when recent context is automatically injected at session start. Each agent has a different mechanism for injecting text into the system prompt or initial context.

| Agent | Mechanism | File / Hook |
|-------|-----------|-------------|
| **Claude Code** | SessionStart hook returns `{ "systemMessage": "..." }` | `hooks/session-start.js` |
| **Cursor** | Rules files loaded at session start | `.cursor/rules` or Cursor Settings > Rules |
| **Windsurf** | Rules file loaded at session start | `.windsurfrules` in project root |
| **Cline** | Custom instructions or rules file | `.clinerules` in project root |
| **Codex CLI** | Agent instructions file | `AGENTS.md` or `codex.md` in project root |
| **Zed** | System prompt in assistant panel | Zed assistant configuration |
| **Aider** | Read-only context files | `--read` flag or `.aider.conf.yml` |

### Wrapper script pattern (agents without hooks)

For agents that don't support lifecycle hooks, use a wrapper script that fetches recent memories, writes them to the agent's rules/context file, then launches the agent:

```bash
#!/bin/bash
# inject-memory.sh — Run before starting your agent
PROJECT=$(basename "$PWD")

# Fetch recent memories
MEMORIES=$(curl -s -X POST localhost:3377/api/observations/search \
  -H 'Content-Type: application/json' \
  -d "{\"query\": \"recent work\", \"project\": \"$PROJECT\", \"limit\": 10}" \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
lines = ['# Recent Memory']
for obs in data.get('observations', []):
    lines.append(f'- [{obs[\"type\"]}] {obs[\"title\"]}')
    if obs.get('narrative'):
        lines.append(f'  {obs[\"narrative\"][:200]}')
print('\n'.join(lines))
")

# Inject into agent's rules file (example: Cursor)
RULES_FILE=".cursor/rules"
mkdir -p .cursor

# Preserve existing rules, append memory section
if [ -f "$RULES_FILE" ]; then
  # Remove old memory section if present
  sed -i '' '/^# Recent Memory$/,/^# /{ /^# Recent Memory$/d; /^# /!d; }' "$RULES_FILE" 2>/dev/null
fi
echo "$MEMORIES" >> "$RULES_FILE"

echo "Injected ${#MEMORIES} bytes of memory context into $RULES_FILE"
```

Adapt the `RULES_FILE` path for your agent:
- Cursor: `.cursor/rules`
- Windsurf: `.windsurfrules`
- Cline: `.clinerules`
- Codex: `AGENTS.md`

---

## 7. Extending the Installer for New Agents

The installer (`install.js`) uses an extensible `AGENTS` object to support multiple agent targets. Currently it only configures Claude Code, but adding new agents follows a simple pattern.

### AGENTS object structure

```javascript
const AGENTS = {
  claude: {
    detect: () => fs.existsSync(path.join(HOME, '.claude')),
    hooksDir: path.join(HOME, '.claude', 'hooks'),
    settingsFile: path.join(HOME, '.claude', 'settings.json'),
    mcpFile: path.join(HOME, '.claude', '.mcp.json'),
    skillsDir: path.join(HOME, '.claude', 'skills'),
    hookEntries: [
      {
        event: 'PostToolUse',
        entry: {
          matcher: 'Read|Edit|Write|Bash|Grep|Glob|NotebookEdit|WebFetch|WebSearch',
          hooks: [{
            type: 'command',
            command: 'node ~/.claude/hooks/agent-memory-post-tool-use.js',
            timeout: 5,
          }],
        },
      },
      // SessionStart and Stop entries follow the same shape
    ],
  },
};
```

### Field reference

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `detect` | yes | `() => boolean` | Returns true if agent is installed on this machine |
| `mcpFile` | yes | `string` | Path to MCP config file (JSON) |
| `hooksDir` | no | `string` | Directory where hook scripts are symlinked |
| `settingsFile` | no | `string` | Agent settings file for registering hooks |
| `skillsDir` | no | `string` | Directory for skills/commands |
| `hookEntries` | no | `array` | Hook registrations (event + matcher + command) |

### Adding a new agent (template)

Agents that only support MCP (no hooks) need just `detect` and `mcpFile`:

```javascript
const AGENTS = {
  // ... existing agents ...

  cursor: {
    detect: () => fs.existsSync(path.join(HOME, '.cursor')),
    mcpFile: path.join(HOME, '.cursor', 'mcp.json'),
    // No hooks, skills, or settings — Cursor only supports MCP
  },

  windsurf: {
    detect: () => fs.existsSync(path.join(HOME, '.codeium')),
    mcpFile: path.join(HOME, '.codeium', 'windsurf', 'mcp_config.json'),
  },
};
```

The installer's `registerMCP()` function reads the target `mcpFile`, merges in the `agent-memory` server entry, and writes it back. The JSON format (`{ "mcpServers": { ... } }`) works for most agents.

**Note**: Codex CLI uses TOML format and VS Code Copilot uses `"servers"` instead of `"mcpServers"`. Adding these agents requires custom `registerMCP()` logic (not yet implemented — contributions welcome).

---

## 8. Queue Payload Reference

The `/api/queue` endpoint accepts tool call data for asynchronous processing. The background worker:
1. Dequeues items atomically (`FOR UPDATE SKIP LOCKED`)
2. Runs a local LLM (Qwen2.5-1.5B) to extract structured observations
3. Falls back to Anthropic Haiku if local LLM unavailable
4. Generates a 768-dim embedding via sentence-transformers
5. Stores the observation with vector in PostgreSQL

### Full schema

```json
{
  "session_id": "required-string",
  "tool_name": "optional-string",
  "tool_input": {},
  "tool_response_preview": "optional-string (truncated to 2000 chars)",
  "cwd": "optional-string",
  "last_user_message": "optional-string"
}
```

### Field details

| Field | Type | Max Length | Description |
|-------|------|-----------|-------------|
| `session_id` | string | — | Session ID (must exist or auto-created via cwd) |
| `tool_name` | string | — | Tool/action name (e.g. `"file_edit"`, `"bash"`, `"Read"`) |
| `tool_input` | object | — | Tool arguments (serialized to JSON for storage) |
| `tool_response_preview` | string | 2000 chars | First portion of tool output |
| `cwd` | string | — | Working directory (project derived from basename) |
| `last_user_message` | string | — | User prompt that triggered this action |

### What the LLM extracts

The background worker produces a structured observation with:
- **title**: One-line summary of what happened
- **type**: Classification (see types reference below)
- **narrative**: Detailed description
- **facts**: Concrete facts as bullet points
- **concepts**: Conceptual tags
- **files_read** / **files_modified**: File paths involved

Low-value tool calls (listing tools, task management, plan mode) are automatically skipped.

---

## 9. Observation Types Reference

The system classifies observations into these types:

| Type | When to use |
|------|------------|
| `decision` | Architectural or design choice was made |
| `bugfix` | A bug was identified and fixed |
| `feature` | New functionality was built |
| `refactor` | Existing code was restructured without changing behavior |
| `discovery` | Something was learned or investigated (default) |
| `change` | A modification that doesn't fit other categories |
| `pattern` | A reusable pattern or approach was identified |
| `gotcha` | A pitfall, caveat, or non-obvious behavior was found |

### Concept tags

Observations can also have conceptual tags:

| Concept | Meaning |
|---------|---------|
| `how-it-works` | Explains internal mechanics |
| `why-it-exists` | Documents rationale |
| `what-changed` | Describes a delta |
| `problem-solution` | Pairs a problem with its fix |
| `gotcha` | Non-obvious caveat |
| `pattern` | Reusable approach |
| `trade-off` | Documents a compromise |

---

## 10. Database Schema

All tables use the `mem_` prefix to avoid collisions in shared databases.

| Table | Purpose |
|-------|---------|
| `embedding_models` | Registry of embedding models used |
| `mem_projects` | Auto-created from working directory |
| `mem_sessions` | One per agent session |
| `mem_observations` | Core memory with title, narrative, embedding |
| `mem_observation_queue` | Async processing queue |
| `mem_schema_migrations` | Tracks applied migrations |

### Search strategy

Hybrid search using **Reciprocal Rank Fusion (RRF)** with k=60:
1. **Vector search** — cosine similarity via pgvector HNSW index (768-dim)
2. **Full-text search** — PostgreSQL tsvector with weighted fields (title=A, subtitle=B, narrative=C, raw_text=D)
3. **Fusion** — `score = sum(1/(60+rank))` across both result sets

### Direct SQL (advanced)

For agents that can query Postgres directly:

```sql
-- Vector similarity search
SELECT id, title, type, 1 - (embedding <=> $1::vector) as similarity
FROM mem_observations
WHERE embedding IS NOT NULL
ORDER BY embedding <=> $1::vector
LIMIT 10;

-- Full-text search
SELECT id, title, type, ts_rank(tsv, plainto_tsquery('english', 'JWT auth')) as rank
FROM mem_observations
WHERE tsv @@ plainto_tsquery('english', 'JWT auth')
ORDER BY rank DESC
LIMIT 10;
```

---

## 11. Troubleshooting

### Server won't start

```bash
# Check if port is already in use
lsof -i :3377

# Check Docker
docker ps | grep agent-memory

# View server logs
tail -50 /path/to/agent-memory/logs/server.log
```

### MCP server can't connect to database

The MCP server reads `.env` from the same directory as `mcp_server.py`. Verify:

```bash
# Test the database connection directly
source /path/to/agent-memory/.venv/bin/activate
python -c "import asyncio, asyncpg; asyncio.run(asyncpg.connect('postgresql://agentmem:pass@localhost:5433/agent_memory'))"
```

### Embeddings not working

```bash
# Test embedding model
source /path/to/agent-memory/.venv/bin/activate
python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5'); print(len(m.encode('test')))"
# Expected: 768
```

### Queue items stuck in "pending"

```bash
# Check queue status
curl localhost:3377/api/admin/stats | python3 -m json.tool

# Server logs show worker activity
tail -f /path/to/agent-memory/logs/server.log | grep -i queue
```

### Explore the API interactively

The FastAPI server includes auto-generated API documentation:

- **Swagger UI**: [localhost:3377/docs](http://localhost:3377/docs) — interactive API explorer with "Try it out" buttons
- **ReDoc**: [localhost:3377/redoc](http://localhost:3377/redoc) — clean reference documentation
