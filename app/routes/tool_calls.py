"""API routes for tool call data from mem_tool_calls table."""

import json
import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from app.db import get_pool
from app.dataset_exports import fetch_tool_call_rows, build_dataset_records
from app.project import project_path_filter
from app.redact import redact_json, redact_text
from app.training_export_guide import build_training_export_guide

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/tool-calls")
async def list_tool_calls(
    project: str | None = None,
    tool_name: str | None = None,
    success: bool | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    """List tool calls with optional filters."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        pidx = 1

        if project:
            if "/" in project:
                clause, pidx = project_path_filter(pidx)
                conditions.append(clause)
                params.extend([project, project, project])
            else:
                conditions.append(f"p.name = ${pidx}")
                params.append(project)
                pidx += 1

        if tool_name:
            conditions.append(f"tc.tool_name = ${pidx}")
            params.append(tool_name)
            pidx += 1

        if success is not None:
            conditions.append(f"tc.tool_success = ${pidx}")
            params.append(success)
            pidx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        params.extend([limit, offset])
        rows = await conn.fetch(f"""
            SELECT tc.id, tc.session_id, tc.project_id, p.name as project_name,
                   tc.tool_name, tc.tool_input, tc.tool_response_preview,
                   tc.tool_success, tc.tool_error, tc.prompt_text,
                   tc.cwd, tc.source_system, tc.source_mode, tc.source_agent,
                   tc.observation_id, tc.created_at
            FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            {where}
            ORDER BY tc.created_at DESC
            LIMIT ${pidx} OFFSET ${pidx + 1}
        """, *params)

        return [_row_to_tool_call(r) for r in rows]


@router.get("/api/tool-calls/stats")
async def tool_call_stats(project: str | None = None):
    """Aggregate tool call statistics, optionally filtered by project."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        project_filter = ""
        params: list = []
        pidx = 1
        if project:
            if "/" in project:
                clause, _ = project_path_filter(pidx)
                project_filter = f"WHERE {clause}"
                params = [project, project, project]
            else:
                project_filter = "WHERE p.name = $1"
                params = [project]

        # Tool frequency
        by_tool = await conn.fetch(f"""
            SELECT tc.tool_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN tc.tool_success = true THEN 1 ELSE 0 END) as success,
                   SUM(CASE WHEN tc.tool_success = false THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN tc.tool_error IS NOT NULL THEN 1 ELSE 0 END) as errors
            FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            {project_filter}
            GROUP BY tc.tool_name
            ORDER BY total DESC
        """, *params)

        # By project
        by_project = await conn.fetch("""
            SELECT p.name as project_name, COUNT(*) as total
            FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            GROUP BY p.name
            ORDER BY total DESC
            LIMIT 30
        """)

        # By agent
        by_agent = await conn.fetch(f"""
            SELECT tc.source_agent, COUNT(*) as total
            FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            {project_filter}
            GROUP BY tc.source_agent
            ORDER BY total DESC
        """, *params)

        # Recent activity (last 7 days, by day)
        by_day = await conn.fetch(f"""
            SELECT DATE(tc.created_at) as day, COUNT(*) as total
            FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            {project_filter}
            AND tc.created_at > NOW() - INTERVAL '7 days'
            GROUP BY DATE(tc.created_at)
            ORDER BY day DESC
        """, *params) if project_filter else await conn.fetch("""
            SELECT DATE(tc.created_at) as day, COUNT(*) as total
            FROM mem_tool_calls tc
            WHERE tc.created_at > NOW() - INTERVAL '7 days'
            GROUP BY DATE(tc.created_at)
            ORDER BY day DESC
        """)

        total = await conn.fetchval(f"""
            SELECT COUNT(*) FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            {project_filter}
        """, *params)

        return {
            "total": total,
            "by_tool": [dict(r) for r in by_tool],
            "by_project": [dict(r) for r in by_project],
            "by_agent": [dict(r) for r in by_agent],
            "by_day": [{"day": str(r["day"]), "total": r["total"]} for r in by_day],
        }


@router.get("/api/tool-calls/export")
async def export_tool_calls(
    project: str | None = None,
    success: bool | None = None,
    format: str = Query(default="jsonl", pattern="^(jsonl|csv)$"),
):
    """Export tool calls for fine-tuning datasets.

    Returns JSONL (one JSON object per line) or CSV.
    Filters: project (name), success (true/false to exclude errors).
    """
    import csv
    import io

    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        pidx = 1

        if project:
            if "/" in project:
                clause, pidx = project_path_filter(pidx)
                conditions.append(clause)
                params.extend([project, project, project])
            else:
                conditions.append(f"p.name = ${pidx}")
                params.append(project)
                pidx += 1

        if success is not None:
            conditions.append(f"tc.tool_success = ${pidx}")
            params.append(success)
            pidx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        rows = await conn.fetch(f"""
            SELECT tc.id, tc.tool_name, tc.tool_input, tc.tool_response_preview,
                   tc.tool_success, tc.tool_error, tc.prompt_text,
                   tc.source_agent, p.name as project_name, tc.created_at
            FROM mem_tool_calls tc
            JOIN mem_projects p ON p.id = tc.project_id
            {where}
            ORDER BY tc.created_at ASC
        """, *params)

        # Redact secrets at the export boundary. Ingest-side redaction is the
        # primary defense, but historical rows pre-date some patterns (e.g.
        # Slack webhooks landed in the v5 pilot dataset) and any new pattern
        # added to redact.py can re-scrub older data on export. tool_input is
        # nested JSON (auth headers, query params); the other text fields are
        # flat strings.
        def _redact_row(r):
            return {
                "tool_input": redact_json(r["tool_input"]) if r["tool_input"] else None,
                "tool_response_preview": redact_text(r["tool_response_preview"]),
                "tool_error": redact_text(r["tool_error"]),
                "prompt_text": redact_text(r["prompt_text"]),
            }

        if format == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["id", "tool_name", "tool_input", "tool_response_preview",
                             "tool_success", "tool_error", "source_agent", "project", "created_at"])
            for r in rows:
                red = _redact_row(r)
                writer.writerow([
                    r["id"], r["tool_name"],
                    json.dumps(red["tool_input"]) if red["tool_input"] else "",
                    (red["tool_response_preview"] or "")[:500],
                    r["tool_success"], red["tool_error"] or "",
                    r["source_agent"] or "", r["project_name"],
                    r["created_at"].isoformat() if r["created_at"] else "",
                ])
            return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                                     headers={"Content-Disposition": "attachment; filename=tool_calls.csv"})

        # JSONL
        lines = []
        for r in rows:
            red = _redact_row(r)
            lines.append(json.dumps({
                "id": r["id"],
                "tool_name": r["tool_name"],
                "tool_input": red["tool_input"],
                "tool_response_preview": red["tool_response_preview"],
                "tool_success": r["tool_success"],
                "tool_error": red["tool_error"],
                "prompt_text": red["prompt_text"],
                "source_agent": r["source_agent"],
                "project": r["project_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }, default=str))
        return PlainTextResponse("\n".join(lines), media_type="application/jsonl",
                                 headers={"Content-Disposition": "attachment; filename=tool_calls.jsonl"})


@router.get("/api/tool-calls/export/dataset")
async def export_training_dataset(
    dataset_type: str = Query(default="sft", pattern="^(sft|trajectory|preference)$"),
    project: str | None = None,
    include_errors: bool = False,
    include_observations: bool = True,
    min_reward: float | None = None,
    max_reward: float | None = None,
    format: str = Query(default="json", pattern="^(json|jsonl)$"),
    limit: int = Query(default=2000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
):
    """Export training-ready datasets from tool calls.

    - sft: prompt -> tool call -> tool output
    - trajectory: tool run + optional linked observation + outcome
    - preference: chosen/rejected pairs for DPO/RM style training
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await fetch_tool_call_rows(
            conn,
            project=project,
            limit=limit,
            offset=offset,
        )
        dataset = build_dataset_records(
            rows,
            dataset_type=dataset_type,
            include_errors=include_errors,
            include_observations=include_observations,
            min_reward=min_reward,
            max_reward=max_reward,
        )

    if format == "jsonl":
        body = "\n".join(json.dumps(item, default=str) for item in dataset)
        filename = f"{dataset_type}_dataset.jsonl"
        return PlainTextResponse(
            body,
            media_type="application/jsonl",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return JSONResponse(
        content={
            "dataset_type": dataset_type,
            "count": len(dataset),
            "project": project,
            "include_errors": include_errors,
            "include_observations": include_observations,
            "items": dataset,
        }
    )


@router.get("/api/tool-calls/export/help")
async def export_training_help():
    """Return a primer/help payload for LLM/agent dataset export workflows."""
    return build_training_export_guide()


def _row_to_tool_call(row) -> dict:
    """Convert a database row to a tool call dict."""
    tool_input = row["tool_input"]
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            pass
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "tool_name": row["tool_name"],
        "tool_input": tool_input,
        "tool_response_preview": row["tool_response_preview"],
        "tool_success": row["tool_success"],
        "tool_error": row["tool_error"],
        "prompt_text": row["prompt_text"],
        "cwd": row["cwd"],
        "source_system": row["source_system"],
        "source_mode": row["source_mode"],
        "source_agent": row["source_agent"],
        "observation_id": row["observation_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
