#!/usr/bin/env python3
"""
agent-memory MCP server — self-contained stdio MCP server.

Connects directly to Postgres, loads its own embedding model.
No dependency on the FastAPI server or app modules.

Usage in ~/.claude/.mcp.json:
  {
    "mcpServers": {
      "agent-memory": {
        "type": "stdio",
        "command": "/Users/mz/Dropbox/_CODING/agentMemory/.venv/bin/python",
        "args": ["/Users/mz/Dropbox/_CODING/agentMemory/mcp_server.py"]
      }
    }
  }
"""

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ── Config (from env or defaults) ─────────────────────────────

DATABASE_URL = os.environ.get(
    "AGENT_MEMORY_DATABASE_URL",
    "postgresql://wfhub:@localhost:5433/agentic",
)
EMBEDDING_MODEL = os.environ.get(
    "AGENT_MEMORY_EMBEDDING_MODEL",
    "nomic-ai/nomic-embed-text-v1.5",
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
                    "project": {"type": "string", "description": "Filter by project name"},
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
                    "project": {"type": "string", "description": "Filter by project name"},
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
                    "project": {"type": "string", "description": "Project name (uses 'manual' if omitted)"},
                },
                "required": ["text"],
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
        params = [emb_str, query, limit * 2]
        filters = []
        pidx = 4

        if project:
            filters.append(f"p.name = ${pidx}")
            params.append(project)
            pidx += 1
        if obs_type:
            filters.append(f"o.type = ${pidx}")
            params.append(obs_type)
            pidx += 1
        if date_start:
            filters.append(f"o.created_at >= ${pidx}::timestamp")
            params.append(date_start)
            pidx += 1
        if date_end:
            filters.append(f"o.created_at <= ${pidx}::timestamp")
            params.append(date_end)
            pidx += 1

        where = ("AND " + " AND ".join(filters)) if filters else ""

        vec_rows = await conn.fetch(f"""
            SELECT o.id, o.title, o.type, o.created_at, p.name as project_name,
                   1 - (o.embedding <=> $1::vector) as vec_score
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            WHERE o.embedding IS NOT NULL {where}
            ORDER BY o.embedding <=> $1::vector
            LIMIT $3
        """, *params)

        fts_rows = await conn.fetch(f"""
            SELECT o.id, o.title, o.type, o.created_at, p.name as project_name,
                   ts_rank(o.tsv, plainto_tsquery('english', $2)) as fts_score
            FROM mem_observations o
            JOIN mem_projects p ON p.id = o.project_id
            WHERE o.tsv @@ plainto_tsquery('english', $2) {where}
            ORDER BY fts_score DESC
            LIMIT $3
        """, *params)

        # Reciprocal Rank Fusion
        scores = {}
        for rank, row in enumerate(vec_rows):
            scores[row["id"]] = {"row": row, "rrf": 1.0 / (60 + rank)}
        for rank, row in enumerate(fts_rows):
            oid = row["id"]
            if oid in scores:
                scores[oid]["rrf"] += 1.0 / (60 + rank)
            else:
                scores[oid] = {"row": row, "rrf": 1.0 / (60 + rank)}

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

        return [TextContent(type="text", text=json.dumps(results, indent=2))]


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

        return [TextContent(type="text", text=json.dumps(results, indent=2))]


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


# ── Main ──────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
