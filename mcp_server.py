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
from app.dataset_exports import fetch_tool_call_rows, build_dataset_records
from app.training_export_guide import build_training_export_guide

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger(__name__)

VISIBILITY_REMINDER = (
    "\n\n---\n"
    "IMPORTANT: Show the user a brief summary of these memory results. "
    "Do NOT silently consume them. Format as a visible 'Memory recall:' block."
)


def _failure_hint(message: str) -> str:
    """Return a short, actionable hint for failure-like tool output."""
    msg = (message or "").lower()
    if "exit code -1" in msg:
        return "Tool exited with code -1. Retry once and verify the command path, permissions, and runtime dependencies."
    if "timeout" in msg or "timed out" in msg:
        return "Operation timed out. Retry with a narrower query or increase the timeout/retry budget."
    if "failed" in msg or "error" in msg:
        return "Tool reported a failure. Check tool input arguments and service health, then retry."
    return "Tool output indicates a problem. Validate inputs and retry."


def _json_error_payload(message: str, code: str = "TOOL_ERROR") -> str:
    """Standard MCP JSON error payload with an LLM-readable hint."""
    return json.dumps(
        {
            "ok": False,
            "error": message,
            "code": code,
            "hint": _failure_hint(message),
        }
    )


def _contains_failure_signal(message: str) -> bool:
    msg = (message or "").lower()
    signals = (
        "error",
        "failed",
        "exception",
        "traceback",
        "timeout",
        "timed out",
        "exit code -1",
    )
    return any(s in msg for s in signals)


def _annotate_success_content_with_hint(contents: list[TextContent]) -> list[TextContent]:
    """If successful output text contains error-like signals, append a warning hint JSON."""
    out = []
    for item in contents:
        if not isinstance(item, TextContent):
            out.append(item)
            continue
        text = item.text or ""
        # If this is already a structured error payload, don't double-annotate.
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("ok") is False and "hint" in parsed:
                out.append(item)
                continue
        except Exception:
            pass

        if _contains_failure_signal(text):
            warning = json.dumps(
                {
                    "ok": True,
                    "warning": {
                        "code": "OUTPUT_CONTAINS_ERROR_SIGNAL",
                        "hint": _failure_hint(text),
                    },
                }
            )
            out.append(TextContent(type="text", text=f"{text}\n\n{warning}"))
        else:
            out.append(item)
    return out

# ── Config (from env or defaults) ─────────────────────────────


def _build_database_url():
    """Build DATABASE_URL from components or use explicit override."""
    explicit = os.environ.get("DATABASE_URL") or os.environ.get("AGENT_MEMORY_DATABASE_URL")
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "agentmem")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "agent_memory")
    pw = f":{password}" if password else ""
    return f"postgresql://{user}{pw}@{host}:{port}/{db}"


DATABASE_URL = _build_database_url()
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL",
    os.environ.get("AGENT_MEMORY_EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v1.5"),
)
MCP_EMBED_TIMEOUT_SECONDS = max(
    0.1,
    float(os.environ.get("AGENT_MEMORY_MCP_EMBED_TIMEOUT_SECONDS", "2.5")),
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


async def try_embed(text: str) -> list[float] | None:
    """Best-effort embedding with timeout to avoid MCP tool stalls/timeouts."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, embed_sync, text),
            timeout=MCP_EMBED_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Embedding timed out after %.1fs; continuing without vector search",
            MCP_EMBED_TIMEOUT_SECONDS,
        )
        return None
    except Exception as e:
        logger.warning("Embedding failed; continuing without vector search: %s", e)
        return None


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
                    "project": {"type": "string", "description": "Project path (full cwd) to scope the memory"},
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
                    "project": {"type": "string", "description": "Project path (full cwd) to scope the lesson. Omit ONLY for truly global lessons."},
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
                    "project": {"type": "string", "description": "Project path (full cwd) to scope results (includes global lessons automatically)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="export_training_dataset",
            description=(
                "Export training datasets from tool-call memory for fine-tuning and reward modeling. "
                "Supports dataset_type=sft|trajectory|preference with project/global scope and reward/error filters."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dataset_type": {"type": "string", "enum": ["sft", "trajectory", "preference"], "default": "sft"},
                    "project": {"type": "string", "description": "Project path/name scope. Omit for global export."},
                    "include_errors": {"type": "boolean", "default": False},
                    "include_observations": {"type": "boolean", "default": True},
                    "min_reward": {"type": "number"},
                    "max_reward": {"type": "number"},
                    "limit": {"type": "integer", "default": 2000},
                    "offset": {"type": "integer", "default": 0},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="training_export_guide",
            description=(
                "Primer/help for agents on collecting clean datasets for fine-tuning and reinforcement learning "
                "from agent-memory via API and MCP."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
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
            result = await _search(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "get_observations":
            result = await _get_observations(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "timeline":
            result = await _timeline(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "save_memory":
            result = await _save_memory(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "create_lesson":
            result = await _create_lesson(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "search_lessons":
            result = await _search_lessons(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "export_training_dataset":
            result = await _export_training_dataset(pool, arguments)
            return _annotate_success_content_with_hint(result)
        elif name == "training_export_guide":
            result = await _training_export_guide()
            return _annotate_success_content_with_hint(result)
        return [TextContent(type="text", text=_json_error_payload(f"Unknown tool: {name}", code="UNKNOWN_TOOL"))]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return [TextContent(type="text", text=_json_error_payload(str(e), code="TOOL_EXCEPTION"))]


async def _search(pool, args):
    query = args["query"]
    project = args.get("project")
    obs_type = args.get("type") or args.get("obs_type")
    limit = min(args.get("limit", 20), 50)
    date_start = args.get("dateStart")
    date_end = args.get("dateEnd")

    embedding = await try_embed(query)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]" if embedding else None

    async with pool.acquire() as conn:
        # Build shared filter clauses (applied to both queries)
        # Each entry is (template_str, [values]) where template uses {pidx} placeholders
        # For path filter: 3 params with consecutive $N; for simple: 1 param
        shared_filters = []
        if project:
            shared_filters.append(("path", project))
        if obs_type:
            shared_filters.append(("o.type = ${}", obs_type))
        if date_start:
            shared_filters.append(("o.created_at >= ${}::timestamp", date_start))
        if date_end:
            shared_filters.append(("o.created_at <= ${}::timestamp", date_end))

        def _apply_shared_filters(params, pidx):
            """Apply shared filters, returns (where_parts, params, pidx)."""
            parts = []
            for entry in shared_filters:
                if entry[0] == "path":
                    val = entry[1]
                    clause = (
                        f"(p.full_path = ${pidx}"
                        f" OR p.full_path LIKE ${pidx+1} || '/%'"
                        f" OR ${pidx+2} LIKE p.full_path || '/%')"
                    )
                    parts.append(clause)
                    params.extend([val, val, val])
                    pidx += 3
                else:
                    tmpl, val = entry
                    parts.append(tmpl.format(pidx))
                    params.append(val)
                    pidx += 1
            return parts, params, pidx

        # --- Vector search (best effort; skipped if embeddings unavailable/slow) ---
        vec_rows = []
        if emb_str:
            vec_params = [emb_str, limit * 2]  # $1=embedding, $2=limit
            vec_where_parts, vec_params, _ = _apply_shared_filters(vec_params, 3)
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
        fts_where_parts, fts_params, _ = _apply_shared_filters(fts_params, 3)
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

        # --- Keyword (ILIKE) search — catches exact substrings FTS misses ---
        keywords = [w for w in query.split()[:6] if len(w) >= 3]
        like_rows = []
        if keywords:
            like_params = [limit * 2]  # $1=limit
            like_conditions = []
            pidx = 2
            for kw in keywords:
                like_conditions.append(f"(o.raw_text ILIKE ${pidx})")
                like_params.append(f"%{kw}%")
                pidx += 1
            # Score = count of matching keywords
            like_score_expr = " + ".join(
                f"CASE WHEN o.raw_text ILIKE ${i+2} THEN 1 ELSE 0 END"
                for i in range(len(keywords))
            )
            like_filter_parts, like_params, _ = _apply_shared_filters(like_params, pidx)
            like_where = ("AND " + " AND ".join(like_filter_parts)) if like_filter_parts else ""

            like_rows = await conn.fetch(f"""
                SELECT o.id, o.title, o.type, o.created_at, p.name as project_name,
                       ({like_score_expr}) as kw_hits
                FROM mem_observations o
                JOIN mem_projects p ON p.id = o.project_id
                WHERE ({" OR ".join(like_conditions)}) {like_where}
                ORDER BY kw_hits DESC, o.created_at DESC
                LIMIT $1
            """, *like_params)

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
        for rank, row in enumerate(like_rows):
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
            best = None
            embedding = await try_embed(query)
            if embedding:
                emb_str = "[" + ",".join(str(v) for v in embedding) + "]"
                best = await conn.fetchrow("""
                    SELECT id FROM mem_observations
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> $1::vector
                    LIMIT 1
                """, emb_str)
            if not best:
                best = await conn.fetchrow("""
                    SELECT id
                    FROM mem_observations
                    WHERE tsv @@ plainto_tsquery('english', $1)
                       OR raw_text ILIKE '%' || $1 || '%'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, query)
            if best:
                anchor_id = best["id"]
            else:
                return [TextContent(type="text", text=_json_error_payload("No observations found matching query", code="NOT_FOUND"))]
        elif not anchor_id:
            return [TextContent(type="text", text=_json_error_payload("Provide either anchor (ID) or query", code="INVALID_ARGUMENT"))]

        anchor = await conn.fetchrow(
            "SELECT session_id, created_at FROM mem_observations WHERE id = $1",
            anchor_id,
        )
        if not anchor:
            return [TextContent(type="text", text=_json_error_payload(f"Observation {anchor_id} not found", code="NOT_FOUND"))]

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

    embedding = await try_embed(text)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]" if embedding else None

    async with pool.acquire() as conn:
        # Get or create project by full_path
        row = await conn.fetchrow("SELECT id FROM mem_projects WHERE full_path = $1", project_name)
        if not row:
            from pathlib import Path as _Path
            basename = _Path(project_name).name or project_name
            row = await conn.fetchrow(
                "INSERT INTO mem_projects (name, full_path) VALUES ($1, $2) "
                "ON CONFLICT (full_path) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                basename, project_name,
            )
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

    embedding = await try_embed(raw_text)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]" if embedding else None

    async with pool.acquire() as conn:
        # Get or create project by full_path
        project_id = None
        if project_name:
            row = await conn.fetchrow("SELECT id FROM mem_projects WHERE full_path = $1", project_name)
            if not row:
                from pathlib import Path as _Path
                basename = _Path(project_name).name or project_name
                row = await conn.fetchrow(
                    "INSERT INTO mem_projects (name, full_path) VALUES ($1, $2) "
                    "ON CONFLICT (full_path) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING id",
                    basename, project_name,
                )
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

    embedding = await try_embed(query)
    emb_str = "[" + ",".join(str(v) for v in embedding) + "]" if embedding else None

    async with pool.acquire() as conn:
        # Build path filter helper for lessons
        def _lesson_path_filter(params, pidx, project_val):
            """Build (l.project_id IS NULL OR path_match) clause for lessons."""
            clause = (
                f"(l.project_id IS NULL"
                f" OR p.full_path = ${pidx}"
                f" OR p.full_path LIKE ${pidx+1} || '/%'"
                f" OR ${pidx+2} LIKE p.full_path || '/%')"
            )
            params.extend([project_val, project_val, project_val])
            return clause, params, pidx + 3

        # --- Vector search (best effort; skipped if embeddings unavailable/slow) ---
        vec_rows = []
        if emb_str:
            vec_params = [emb_str, limit * 2]
            vec_pidx = 3
            vec_where_parts = ["l.active = true"]
            if project:
                clause, vec_params, vec_pidx = _lesson_path_filter(vec_params, vec_pidx, project)
                vec_where_parts.append(clause)
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
        if project:
            clause, fts_params, fts_pidx = _lesson_path_filter(fts_params, fts_pidx, project)
            fts_where_parts.append(clause)
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


async def _export_training_dataset(pool, args):
    dataset_type = args.get("dataset_type", "sft")
    project = args.get("project")
    include_errors = bool(args.get("include_errors", False))
    include_observations = bool(args.get("include_observations", True))
    min_reward = args.get("min_reward")
    max_reward = args.get("max_reward")
    limit = min(int(args.get("limit", 2000)), 10000)
    offset = max(int(args.get("offset", 0)), 0)

    async with pool.acquire() as conn:
        rows = await fetch_tool_call_rows(
            conn,
            project=project,
            limit=limit,
            offset=offset,
        )
        items = build_dataset_records(
            rows,
            dataset_type=dataset_type,
            include_errors=include_errors,
            include_observations=include_observations,
            min_reward=min_reward,
            max_reward=max_reward,
        )

    payload = {
        "dataset_type": dataset_type,
        "count": len(items),
        "project": project,
        "include_errors": include_errors,
        "include_observations": include_observations,
        "items": items,
    }
    return [TextContent(type="text", text=json.dumps(payload, indent=2) + VISIBILITY_REMINDER)]


async def _training_export_guide():
    payload = build_training_export_guide()
    return [TextContent(type="text", text=json.dumps(payload, indent=2) + VISIBILITY_REMINDER)]


# ── Main ──────────────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
