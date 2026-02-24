#!/usr/bin/env python3
"""
agent-memory MCP server — self-contained stdio MCP server.

Connects directly to Postgres, loads its own embedding model.
No dependency on the FastAPI server or app modules.

Registered by install.js in ~/.claude/.mcp.json.
"""

import asyncio
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load .env from the same directory as this script
_script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_script_dir, ".env"))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger(__name__)

VISIBILITY_REMINDER = (
    "\n\n---\n"
    "IMPORTANT: Show the user a brief summary of these memory results. "
    "Do NOT silently consume them. Format as a visible 'Memory recall:' block."
)

# ── Config (from env or defaults) ─────────────────────────────


def _build_database_url():
    """Build DATABASE_URL from components or use explicit override."""
    explicit = os.environ.get("DATABASE_URL") or os.environ.get("AGENT_MEMORY_DATABASE_URL")
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "agentmem")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5433")
    db = os.environ.get("POSTGRES_DB", "agent_memory")
    pw = f":{password}" if password else ""
    return f"postgresql://{user}{pw}@{host}:{port}/{db}"


DATABASE_URL = _build_database_url()
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL",
    os.environ.get("AGENT_MEMORY_EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v1.5"),
)

# ── DB pool (lazy) ────────────────────────────────────────────

_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        import asyncpg
        dsn = DATABASE_URL.replace("postgresql://", "postgres://", 1)
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    return _pool


# ── Embedding model (lazy singleton) ─────────────────────────

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL, trust_remote_code=True)
    return _model


def embed_sync(text: str) -> list[float]:
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


# ── MCP Server ────────────────────────────────────────────────

server = Server("agent-memory")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="memory_search_guide",
            description=(
                "3-LAYER WORKFLOW (ALWAYS FOLLOW):\n"
                "1. search(query) → Get index with IDs (~50-100 tokens/result)\n"
                "2. timeline(anchor=ID) → Get context around interesting results\n"
                "3. get_observations([IDs]) → Fetch full details ONLY for filtered IDs\n"
                "NEVER fetch full details without filtering first. 10x token savings."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search",
            description=(
                "Step 1: Search memory. Returns index with IDs. "
                "Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Semantic search query"},
                    "project": {"type": "string", "description": "Project/folder name — ALWAYS pass to scope results to current project"},
                    "type": {"type": "string", "description": "Filter by type: discovery|bugfix|feature|refactor|decision|change|pattern|gotcha"},
                    "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                    "dateStart": {"type": "string", "description": "Filter from date (ISO format, e.g. 2026-02-01)"},
                    "dateEnd": {"type": "string", "description": "Filter until date (ISO format)"},
                },
                "required": ["query"],
                "additionalProperties": True,
            },
        ),
        Tool(
            name="timeline",
            description=(
                "Step 2: Get context around results. "
                "Params: anchor (observation ID) OR query (finds anchor automatically), depth_before, depth_after, project"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "anchor": {"type": "integer", "description": "Observation ID to center on"},
                    "query": {"type": "string", "description": "Find anchor automatically by searching for this query"},
                    "depth_before": {"type": "integer", "description": "Observations before (default 3)", "default": 3},
                    "depth_after": {"type": "integer", "description": "Observations after (default 3)", "default": 3},
                    "project": {"type": "string", "description": "Project/folder name — pass to scope results to current project"},
                },
            },
        ),
        Tool(
            name="get_observations",
            description=(
                "Step 3: Fetch full details for filtered IDs. "
                "Params: ids (array of observation IDs, required), orderBy, limit, project"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Array of observation IDs to fetch (required)",
                    },
                },
                "required": ["ids"],
                "additionalProperties": True,
            },
        ),
        Tool(
            name="save_memory",
            description="Save a manual memory/observation for semantic search. Use this to remember important information.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Content to remember (required)"},
                    "title": {"type": "string", "description": "Short title (auto-generated from text if omitted)"},
                    "project": {"type": "string", "description": "Project/folder name — ALWAYS pass to scope the memory to the current project"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="create_lesson",
            description=(
                "Create a lesson — a proactive rule that fires BEFORE risky operations. "
                "Unlike observations (passive), lessons are instructions injected at session start "
                "and triggered by PreToolUse hooks. Use after learning from a mistake."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "description": "The instruction/rule (e.g. 'ALWAYS diff dev vs prod config before deploying')"},
                    "title": {"type": "string", "description": "Short title for the lesson"},
                    "severity": {"type": "string", "enum": ["critical", "warning", "info"], "description": "How important (default: warning)", "default": "warning"},
                    "project": {"type": "string", "description": "Project/folder name — ALWAYS pass this to scope the lesson to the current project. Omit ONLY for truly global lessons that apply everywhere."},
                    "trigger_tool": {"type": "string", "description": "Tool to match: Bash, Edit, Write, NotebookEdit (omit for any)"},
                    "trigger_pattern": {"type": "string", "description": "Regex to match against tool input (e.g. 'amplify.*update-app')"},
                },
                "required": ["rule"],
            },
        ),
        Tool(
            name="search_lessons",
            description="Search existing lessons (proactive rules). Use to check if a lesson already exists before creating one. ALWAYS pass project to scope results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "project": {"type": "string", "description": "Project/folder name — ALWAYS pass this to scope results to the current project (includes global lessons automatically)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "memory_search_guide":
            return [TextContent(type="text", text=(
                "3-LAYER WORKFLOW (ALWAYS FOLLOW):\n"
                "1. search(query) → Get index with IDs (~50-100 tokens/result)\n"
                "2. timeline(anchor=ID) → Get context around interesting results\n"
                "3. get_observations([IDs]) → Fetch full details ONLY for filtered IDs\n"
                "NEVER fetch full details without filtering first. 10x token savings."
            ))]
        pool = await get_pool()
        if name == "search":
            return await _search(pool, arguments)
        elif name == "get_observations":
            return await _get_observations(pool, arguments)
        elif name == "timeline":
            return await _timeline(pool, arguments)
        elif name == "save_memory":
            return await _save_memory(pool, arguments)
        elif name == "create_lesson":
            return await _create_lesson(pool, arguments)
        elif name == "search_lessons":
            return await _search_lessons(pool, arguments)
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _search(pool, args):
    query = args["query"]
    project = args.get("project")
    obs_type = args.get("type") or args.get("obs_type")
    limit = min(args.get("limit", 20), 50)
    date_start = args.get("dateStart")
    date_end = args.get("dateEnd")

    # Embed query
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(None, embed_sync, query)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

    async with pool.acquire() as conn:
        # Build shared filter clauses (applied to both queries)
        # Each query builds its own params with its own $N numbering
        shared_filters = []
        shared_values = []
        if project:
            shared_filters.append(("p.name = ${}", project))
        if obs_type:
            shared_filters.append(("o.type = ${}", obs_type))
        if date_start:
            shared_filters.append(("o.created_at >= ${}::timestamp", date_start))
        if date_end:
            shared_filters.append(("o.created_at <= ${}::timestamp", date_end))

        # --- Vector search ---
        vec_params = [emb_str, limit * 2]  # $1=embedding, $2=limit
        vec_pidx = 3
        vec_where_parts = []
        for tmpl, val in shared_filters:
            vec_where_parts.append(tmpl.format(vec_pidx))
            vec_params.append(val)
            vec_pidx += 1
        vec_where = ("AND " + " AND ".join(vec_where_parts)) if vec_where_parts else ""

        vec_rows = await conn.fetch(f"""
            SELECT o.id, o.title, o.type, o.created_at, p.name as project_name,
                   1 - (o.embedding <=> $1::vector) as vec_score
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            WHERE o.embedding IS NOT NULL {vec_where}
            ORDER BY o.embedding <=> $1::vector
            LIMIT $2
        """, *vec_params)

        # --- Full-text search ---
        fts_params = [query, limit * 2]  # $1=query, $2=limit
        fts_pidx = 3
        fts_where_parts = []
        for tmpl, val in shared_filters:
            fts_where_parts.append(tmpl.format(fts_pidx))
            fts_params.append(val)
            fts_pidx += 1
        fts_where = ("AND " + " AND ".join(fts_where_parts)) if fts_where_parts else ""

        fts_rows = await conn.fetch(f"""
            SELECT o.id, o.title, o.type, o.created_at, p.name as project_name,
                   ts_rank(o.tsv, plainto_tsquery('english', $1)) as fts_score
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            WHERE o.tsv @@ plainto_tsquery('english', $1) {fts_where}
            ORDER BY fts_score DESC
            LIMIT $2
        """, *fts_params)

        # Reciprocal Rank Fusion with recency boost
        scores = {}
        for rank, row in enumerate(vec_rows):
            scores[row["id"]] = {"row": row, "rrf": 1.0 / (60 + rank)}
        for rank, row in enumerate(fts_rows):
            oid = row["id"]
            if oid in scores:
                scores[oid]["rrf"] += 1.0 / (60 + rank)
            else:
                scores[oid] = {"row": row, "rrf": 1.0 / (60 + rank)}

        # Apply recency boost: recent observations score higher
        now_utc = datetime.now(timezone.utc)
        for item in scores.values():
            created = item["row"]["created_at"]
            if created:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_days = max((now_utc - created).total_seconds() / 86400, 0)
                # Exponential decay: today=2x, 7d=1.5x, 30d=1.1x, 90d+=1.0x
                boost = 1.0 + math.exp(-age_days / 10.0)
                item["rrf"] *= boost

        ranked = sorted(scores.values(), key=lambda x: -x["rrf"])[:limit]

        results = []
        for item in ranked:
            row = item["row"]
            results.append({
                "id": row["id"],
                "title": row["title"],
                "type": row["type"],
                "project": row["project_name"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "score": round(item["rrf"], 4),
            })

        return [TextContent(type="text", text=json.dumps(results, indent=2) + VISIBILITY_REMINDER)]


async def _get_observations(pool, args):
    ids = args["ids"]
    if not ids:
        return [TextContent(type="text", text="[]")]

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT o.id, o.title, o.subtitle, o.type, o.narrative,
                   o.facts, o.concepts, o.files_read, o.files_modified,
                   o.raw_text, o.tool_name, o.created_at,
                   p.name as project_name
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            WHERE o.id = ANY($1)
            ORDER BY o.created_at
        """, ids)

        results = []
        for row in rows:
            results.append({
                "id": row["id"],
                "title": row["title"],
                "subtitle": row["subtitle"],
                "type": row["type"],
                "narrative": row["narrative"],
                "facts": json.loads(row["facts"]) if row["facts"] else [],
                "concepts": json.loads(row["concepts"]) if row["concepts"] else [],
                "files_read": json.loads(row["files_read"]) if row["files_read"] else [],
                "files_modified": json.loads(row["files_modified"]) if row["files_modified"] else [],
                "project": row["project_name"],
                "tool_name": row["tool_name"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            })

        return [TextContent(type="text", text=json.dumps(results, indent=2) + VISIBILITY_REMINDER)]


async def _timeline(pool, args):
    anchor_id = args.get("anchor")
    query = args.get("query")
    before = args.get("depth_before", 3)
    after = args.get("depth_after", 3)

    async with pool.acquire() as conn:
        # If query provided instead of anchor ID, find best match
        if not anchor_id and query:
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(None, embed_sync, query)
            emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
            best = await conn.fetchrow("""
                SELECT id FROM mem_observations
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 1
            """, emb_str)
            if best:
                anchor_id = best["id"]
            else:
                return [TextContent(type="text", text="No observations found matching query")]
        elif not anchor_id:
            return [TextContent(type="text", text="Provide either anchor (ID) or query")]

        anchor = await conn.fetchrow(
            "SELECT session_id, created_at FROM mem_observations WHERE id = $1",
            anchor_id,
        )
        if not anchor:
            return [TextContent(type="text", text=f"Observation {anchor_id} not found")]

        rows = await conn.fetch("""
            (SELECT id, title, type, created_at, 'before' as position
             FROM mem_observations
             WHERE session_id = $1 AND created_at < $2
             ORDER BY created_at DESC LIMIT $3)
            UNION ALL
            (SELECT id, title, type, created_at, 'anchor' as position
             FROM mem_observations WHERE id = $4)
            UNION ALL
            (SELECT id, title, type, created_at, 'after' as position
             FROM mem_observations
             WHERE session_id = $1 AND created_at > $2
             ORDER BY created_at ASC LIMIT $5)
            ORDER BY created_at
        """, anchor["session_id"], anchor["created_at"], before, anchor_id, after)

        results = [{
            "id": row["id"],
            "title": row["title"],
            "type": row["type"],
            "position": row["position"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        } for row in rows]

        return [TextContent(type="text", text=json.dumps(results, indent=2))]


async def _save_memory(pool, args):
    text = args["text"]
    title = args.get("title", text[:80])
    project_name = args.get("project", "manual")

    # Embed
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(None, embed_sync, text)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

    async with pool.acquire() as conn:
        # Get or create project
        row = await conn.fetchrow("SELECT id FROM mem_projects WHERE name = $1", project_name)
        if not row:
            row = await conn.fetchrow("INSERT INTO mem_projects (name) VALUES ($1) RETURNING id", project_name)
        project_id = row["id"]

        # Get or create manual session
        srow = await conn.fetchrow("SELECT id FROM mem_sessions WHERE session_id = 'manual-memories'")
        if not srow:
            srow = await conn.fetchrow(
                "INSERT INTO mem_sessions (session_id, project_id, agent_type, status) VALUES ('manual-memories', $1, 'manual', 'active') RETURNING id",
                project_id,
            )
        session_db_id = srow["id"]

        model_row = await conn.fetchrow("SELECT id FROM embedding_models WHERE is_default = true LIMIT 1")
        model_id = model_row["id"] if model_row else None

        obs_row = await conn.fetchrow("""
            INSERT INTO mem_observations (
                session_id, project_id, title, type, narrative,
                raw_text, embedding, embedding_model_id, created_at
            ) VALUES ($1, $2, $3, 'discovery', $4, $4, $5::vector, $6, now())
            RETURNING id
        """, session_db_id, project_id, title, text, emb_str, model_id)

        return [TextContent(type="text", text=json.dumps({"saved": True, "id": obs_row["id"], "title": title}))]


async def _create_lesson(pool, args):
    rule = args["rule"]
    title = args.get("title", rule[:80])
    severity = args.get("severity", "warning")
    if severity not in ("critical", "warning", "info"):
        severity = "warning"
    project_name = args.get("project")
    trigger_tool = args.get("trigger_tool")
    trigger_pattern = args.get("trigger_pattern")

    raw_text = f"{title}\n{rule}"

    # Embed
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(None, embed_sync, raw_text)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

    async with pool.acquire() as conn:
        # Get or create project
        project_id = None
        if project_name:
            row = await conn.fetchrow("SELECT id FROM mem_projects WHERE name = $1", project_name)
            if not row:
                row = await conn.fetchrow("INSERT INTO mem_projects (name) VALUES ($1) RETURNING id", project_name)
            project_id = row["id"]

        lesson_row = await conn.fetchrow("""
            INSERT INTO mem_lessons (
                project_id, title, rule, severity,
                trigger_tool, trigger_pattern,
                embedding, raw_text
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8)
            RETURNING id
        """, project_id, title, rule, severity,
            trigger_tool, trigger_pattern,
            emb_str, raw_text)

        return [TextContent(type="text", text=json.dumps({
            "saved": True,
            "id": lesson_row["id"],
            "title": title,
            "severity": severity,
            "project": project_name,
        }))]


async def _search_lessons(pool, args):
    query = args["query"]
    project = args.get("project")
    limit = min(args.get("limit", 10), 50)

    # Embed query
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(None, embed_sync, query)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

    async with pool.acquire() as conn:
        # Build filters
        shared_filters = []
        shared_values = []
        if project:
            shared_filters.append(("(l.project_id IS NULL OR p.name = ${})", project))

        # --- Vector search ---
        vec_params = [emb_str, limit * 2]
        vec_pidx = 3
        vec_where_parts = ["l.active = true"]
        for tmpl, val in shared_filters:
            vec_where_parts.append(tmpl.format(vec_pidx))
            vec_params.append(val)
            vec_pidx += 1
        vec_where = " AND ".join(vec_where_parts)

        vec_rows = await conn.fetch(f"""
            SELECT l.id, l.title, l.rule, l.severity, l.trigger_tool,
                   l.trigger_pattern, l.trigger_count, l.created_at,
                   p.name as project_name,
                   1 - (l.embedding <=> $1::vector) as vec_score
            FROM mem_lessons l
            LEFT JOIN mem_projects p ON p.id = l.project_id
            WHERE l.embedding IS NOT NULL AND {vec_where}
            ORDER BY l.embedding <=> $1::vector
            LIMIT $2
        """, *vec_params)

        # --- Full-text search ---
        fts_params = [query, limit * 2]
        fts_pidx = 3
        fts_where_parts = ["l.active = true"]
        for tmpl, val in shared_filters:
            fts_where_parts.append(tmpl.format(fts_pidx))
            fts_params.append(val)
            fts_pidx += 1
        fts_where = " AND ".join(fts_where_parts)

        fts_rows = await conn.fetch(f"""
            SELECT l.id, l.title, l.rule, l.severity, l.trigger_tool,
                   l.trigger_pattern, l.trigger_count, l.created_at,
                   p.name as project_name,
                   ts_rank(l.tsv, plainto_tsquery('english', $1)) as fts_score
            FROM mem_lessons l
            LEFT JOIN mem_projects p ON p.id = l.project_id
            WHERE l.tsv @@ plainto_tsquery('english', $1) AND {fts_where}
            ORDER BY fts_score DESC
            LIMIT $2
        """, *fts_params)

        # RRF fusion
        scores = {}
        for rank, row in enumerate(vec_rows):
            scores[row["id"]] = {"row": row, "rrf": 1.0 / (60 + rank)}
        for rank, row in enumerate(fts_rows):
            lid = row["id"]
            if lid in scores:
                scores[lid]["rrf"] += 1.0 / (60 + rank)
            else:
                scores[lid] = {"row": row, "rrf": 1.0 / (60 + rank)}

        ranked = sorted(scores.values(), key=lambda x: -x["rrf"])[:limit]

        results = []
        for item in ranked:
            row = item["row"]
            results.append({
                "id": row["id"],
                "title": row["title"],
                "rule": row["rule"],
                "severity": row["severity"],
                "project": row["project_name"],
                "trigger_tool": row["trigger_tool"],
                "trigger_pattern": row["trigger_pattern"],
                "trigger_count": row["trigger_count"],
                "score": round(item["rrf"], 4),
            })

        return [TextContent(type="text", text=json.dumps(results, indent=2))]


# ── Main ──────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
