"""Training dataset export builders shared by API routes and MCP tools."""

from __future__ import annotations

import json
import re
from typing import Any

from app.redact import redact_json, redact_text


_PROBLEM_PATTERNS = [
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"no such file or directory", re.IGNORECASE),
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"traceback", re.IGNORECASE),
    re.compile(r"^error[:\s]", re.IGNORECASE),
    re.compile(r"^fatal:", re.IGNORECASE),
    re.compile(r"exit code [1-9]", re.IGNORECASE),
    re.compile(r"path escapes workspace", re.IGNORECASE),
]


def _is_problematic(row: dict[str, Any]) -> bool:
    if row.get("tool_success") is False:
        return True
    if row.get("tool_error"):
        return True
    preview = row.get("tool_response_preview") or ""
    head = preview[:500]
    return any(p.search(head) for p in _PROBLEM_PATTERNS)


def _compute_reward(row: dict[str, Any]) -> float:
    reward = 1.0 if row.get("tool_success") else -1.0
    if row.get("observation_id"):
        reward += 0.25
    else:
        reward -= 0.10
    if row.get("session_status") == "completed":
        reward += 0.10
    if row.get("tool_error"):
        reward -= 0.25
    if _is_problematic(row):
        reward -= 0.25
    # Keep reward bounded but still expressive.
    return round(max(-2.0, min(2.0, reward)), 3)


def _norm_prompt(prompt_text: str | None, fallback: str | None) -> str:
    text = (prompt_text or fallback or "").strip()
    return text


def _parse_jsonb(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _base_record(row: dict[str, Any], include_observations: bool) -> dict[str, Any]:
    prompt = _norm_prompt(row.get("prompt_text"), row.get("last_user_message"))
    # Redact at the dataset-record boundary: every downstream export type
    # (sft, trajectory, preference) flows through this function, so one
    # patch here covers all of them. Ingest-side redaction is the primary
    # defense; this catches historical rows ingested before a pattern was
    # added (e.g. Slack webhooks that landed in the v5 pilot dataset).
    rec = {
        "tool_call_id": row["id"],
        "project": row.get("project_name"),
        "project_path": row.get("project_path"),
        "session_id": row.get("session_external_id"),
        "source_agent": row.get("source_agent"),
        "source_system": row.get("source_system"),
        "source_mode": row.get("source_mode"),
        "prompt_text": redact_text(prompt),
        "tool_name": row.get("tool_name"),
        "tool_input": redact_json(_parse_jsonb(row.get("tool_input"))),
        "tool_response_preview": redact_text(row.get("tool_response_preview")),
        "tool_success": row.get("tool_success"),
        "tool_error": redact_text(row.get("tool_error")),
        "reward": _compute_reward(row),
        "is_problematic": _is_problematic(row),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
    }
    if include_observations:
        rec["observation"] = None
        if row.get("observation_id"):
            rec["observation"] = {
                "id": row.get("observation_id"),
                "title": row.get("obs_title"),
                "type": row.get("obs_type"),
                "narrative": row.get("obs_narrative"),
                "facts": _parse_jsonb(row.get("obs_facts")) or [],
                "concepts": _parse_jsonb(row.get("obs_concepts")) or [],
            }
    return rec


async def fetch_tool_call_rows(
    conn,
    *,
    project: str | None = None,
    limit: int = 2000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where_parts: list[str] = []
    pidx = 1

    if project:
        where_parts.append(
            f"(p.full_path = ${pidx} OR p.name = ${pidx+1} "
            f"OR p.full_path LIKE ${pidx+2} || '/%' OR ${pidx+3} LIKE p.full_path || '/%')"
        )
        params.extend([project, project, project, project])
        pidx += 4

    params.extend([limit, offset])
    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    rows = await conn.fetch(
        f"""
        SELECT tc.id, tc.tool_name, tc.tool_input, tc.tool_response_preview,
               tc.tool_success, tc.tool_error, tc.prompt_text,
               tc.source_system, tc.source_mode, tc.source_agent, tc.observation_id,
               tc.created_at,
               p.name AS project_name, p.full_path AS project_path,
               s.session_id AS session_external_id, s.status AS session_status,
               q.last_user_message,
               o.title AS obs_title, o.type AS obs_type, o.narrative AS obs_narrative,
               o.facts AS obs_facts, o.concepts AS obs_concepts
        FROM mem_tool_calls tc
        JOIN mem_projects p ON p.id = tc.project_id
        JOIN mem_sessions s ON s.id = tc.session_id
        LEFT JOIN mem_observation_queue q ON q.id = tc.queue_id
        LEFT JOIN mem_observations o ON o.id = tc.observation_id
        {where}
        ORDER BY tc.created_at ASC
        LIMIT ${pidx} OFFSET ${pidx + 1}
        """,
        *params,
    )
    return [dict(r) for r in rows]


def build_dataset_records(
    rows: list[dict[str, Any]],
    *,
    dataset_type: str,
    include_errors: bool,
    include_observations: bool,
    min_reward: float | None = None,
    max_reward: float | None = None,
) -> list[dict[str, Any]]:
    dataset_type = dataset_type.lower()
    if dataset_type not in {"sft", "trajectory", "preference"}:
        raise ValueError(f"Unsupported dataset_type: {dataset_type}")

    base_records = [_base_record(r, include_observations) for r in rows]

    filtered: list[dict[str, Any]] = []
    for rec in base_records:
        if not include_errors and rec["is_problematic"]:
            continue
        reward = rec["reward"]
        if min_reward is not None and reward < min_reward:
            continue
        if max_reward is not None and reward > max_reward:
            continue
        filtered.append(rec)

    if dataset_type == "sft":
        return [
            {
                "dataset_type": "sft",
                "input": {
                    "prompt_text": rec["prompt_text"],
                    "tool_name": rec["tool_name"],
                    "tool_input": rec["tool_input"],
                },
                "output": {
                    "tool_response_preview": rec["tool_response_preview"],
                },
                "meta": {
                    "tool_call_id": rec["tool_call_id"],
                    "project": rec["project"],
                    "session_id": rec["session_id"],
                    "source_agent": rec["source_agent"],
                    "reward": rec["reward"],
                    "is_problematic": rec["is_problematic"],
                    "created_at": rec["created_at"],
                },
            }
            for rec in filtered
        ]

    if dataset_type == "trajectory":
        return [
            {
                "dataset_type": "trajectory",
                "trajectory": [
                    {
                        "prompt_text": rec["prompt_text"],
                        "tool_name": rec["tool_name"],
                        "tool_input": rec["tool_input"],
                        "tool_response_preview": rec["tool_response_preview"],
                        "observation": rec.get("observation"),
                    }
                ],
                "outcome": {
                    "reward": rec["reward"],
                    "tool_success": rec["tool_success"],
                    "tool_error": rec["tool_error"],
                    "is_problematic": rec["is_problematic"],
                },
                "meta": {
                    "tool_call_id": rec["tool_call_id"],
                    "project": rec["project"],
                    "session_id": rec["session_id"],
                    "created_at": rec["created_at"],
                },
            }
            for rec in filtered
        ]

    # Preference dataset: pair records by (project, tool_name, normalized prompt)
    grouped: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = {}
    for rec in filtered:
        prompt_key = (rec["prompt_text"] or "").strip().lower()[:200]
        key = (rec["project"] or "", rec["tool_name"] or "", prompt_key)
        grouped.setdefault(key, {"pos": [], "neg": []})
        if rec["reward"] >= 0 and rec["tool_success"]:
            grouped[key]["pos"].append(rec)
        elif rec["reward"] < 0 or rec["is_problematic"]:
            grouped[key]["neg"].append(rec)

    pairs: list[dict[str, Any]] = []
    for (project, tool_name, prompt_key), bucket in grouped.items():
        positives = bucket["pos"]
        negatives = bucket["neg"]
        if not positives or not negatives:
            continue
        pair_count = min(len(positives), len(negatives))
        for idx in range(pair_count):
            chosen = positives[idx]
            rejected = negatives[idx]
            pairs.append(
                {
                    "dataset_type": "preference",
                    "prompt_text": chosen["prompt_text"] or rejected["prompt_text"],
                    "tool_name": tool_name,
                    "chosen": {
                        "tool_call_id": chosen["tool_call_id"],
                        "tool_input": chosen["tool_input"],
                        "tool_response_preview": chosen["tool_response_preview"],
                        "reward": chosen["reward"],
                        "observation": chosen.get("observation"),
                    },
                    "rejected": {
                        "tool_call_id": rejected["tool_call_id"],
                        "tool_input": rejected["tool_input"],
                        "tool_response_preview": rejected["tool_response_preview"],
                        "reward": rejected["reward"],
                        "observation": rejected.get("observation"),
                    },
                    "meta": {
                        "project": project,
                        "prompt_key": prompt_key,
                    },
                }
            )
    return pairs

