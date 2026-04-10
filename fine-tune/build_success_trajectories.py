#!/usr/bin/env python3
"""Build multi-step successful tool-call trajectories for RL-style training.

Output shape:
- prompt_text
- tools[] (ordered tool calls)
- signals (qa/docs/plan/review/test)
- reward

Raw outputs go to data/raw/, curated RL datasets go to data/processed/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings


@dataclass
class Step:
    tool_name: str | None
    tool_input: Any
    tool_response_preview: str | None
    tool_success: bool | None
    tool_error: str | None
    created_at: datetime | None


def _load_profile(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(s: Any) -> str:
    if s is None:
        return ""
    if isinstance(s, str):
        return s
    return json.dumps(s, ensure_ascii=False)


def _contains_any(text: str, patterns: list[str]) -> bool:
    lower = text.lower()
    return any(p.lower() in lower for p in patterns)


def _signal_flags(prompt: str, steps: list[Step], profile: dict[str, Any]) -> dict[str, bool]:
    sig = profile["signals"]
    blob = "\n".join(
        [
            prompt,
            *[
                f"{_norm(s.tool_name)}\n{_norm(s.tool_input)}\n{_norm(s.tool_response_preview)}\n{_norm(s.tool_error)}"
                for s in steps
            ],
        ]
    )

    has_qa = _contains_any(blob, sig["qa_patterns"])
    has_docs = _contains_any(blob, sig["docs_patterns"])
    has_plan = _contains_any(blob, sig["plan_patterns"])
    has_review = _contains_any(blob, sig["review_patterns"])
    has_test = any(
        _contains_any(_norm(s.tool_input), ["pytest", "playwright", "npm test", "integration test"]) for s in steps
    )

    return {
        "has_qa_signal": has_qa,
        "has_docs_signal": has_docs,
        "has_plan_signal": has_plan,
        "has_review_signal": has_review,
        "has_test_signal": has_test,
    }


def _compute_reward(steps: list[Step], flags: dict[str, bool], profile: dict[str, Any]) -> float:
    w = profile["weights"]
    total = 0.0

    successes = sum(1 for s in steps if s.tool_success)
    ratio = (successes / len(steps)) if steps else 0.0

    if ratio == 1.0:
        total += w["all_tools_success"]
    total += ratio * w["tool_success_ratio"]

    for key in [
        "has_qa_signal",
        "has_docs_signal",
        "has_plan_signal",
        "has_review_signal",
        "has_test_signal",
    ]:
        if flags.get(key):
            total += w[key]

    step_bonus = min(len(steps) * w["tool_count_bonus_per_step"], w["tool_count_bonus_cap"])
    total += step_bonus

    if any((s.tool_success is False) or s.tool_error for s in steps):
        total += w["error_penalty"]

    return round(total, 4)


async def _fetch_rows(
    conn: asyncpg.Connection,
    project: str | None,
    limit: int,
    offset: int,
    strict_project: bool,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    where: list[str] = []
    i = 1

    if project:
        if strict_project:
            where.append(f"(p.full_path = ${i} OR p.name = ${i+1})")
            params.extend([project, project])
            i += 2
        else:
            where.append(
                f"(p.full_path = ${i} OR p.name = ${i+1} OR p.full_path LIKE ${i+2} || '/%' OR ${i+3} LIKE p.full_path || '/%')"
            )
            params.extend([project, project, project, project])
            i += 4

    params.extend([limit, offset])
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    rows = await conn.fetch(
        f"""
        SELECT tc.id, tc.session_id, tc.prompt_text, tc.tool_name, tc.tool_input,
               tc.tool_response_preview, tc.tool_success, tc.tool_error, tc.created_at,
               q.last_user_message
        FROM mem_tool_calls tc
        JOIN mem_projects p ON p.id = tc.project_id
        LEFT JOIN mem_observation_queue q ON q.id = tc.queue_id
        {where_sql}
        ORDER BY tc.session_id, tc.created_at ASC, tc.id ASC
        LIMIT ${i} OFFSET ${i + 1}
        """,
        *params,
    )
    return [dict(r) for r in rows]


def _make_anchor(row: dict[str, Any]) -> str:
    prompt = _norm(row.get("prompt_text")).strip()
    if prompt:
        return prompt
    fallback = _norm(row.get("last_user_message")).strip()
    return fallback


def _build_episodes(rows: list[dict[str, Any]], min_steps: int, max_steps: int) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []

    current_session: Any = None
    current_anchor: str = ""
    current_steps: list[Step] = []

    def flush() -> None:
        nonlocal current_steps, current_anchor
        if len(current_steps) >= min_steps:
            episodes.append(
                {
                    "prompt_text": current_anchor,
                    "tools": [
                        {
                            "tool_name": s.tool_name,
                            "tool_input": s.tool_input,
                            "tool_response_preview": s.tool_response_preview,
                            "tool_success": s.tool_success,
                            "tool_error": s.tool_error,
                            "created_at": s.created_at.isoformat() if s.created_at else None,
                        }
                        for s in current_steps[:max_steps]
                    ],
                }
            )
        current_steps = []
        current_anchor = ""

    for row in rows:
        sid = row["session_id"]
        anchor = _make_anchor(row)

        if current_session is None:
            current_session = sid
            current_anchor = anchor

        new_episode = False
        if sid != current_session:
            new_episode = True
        elif anchor and current_anchor and anchor != current_anchor:
            new_episode = True
        elif anchor and not current_anchor:
            new_episode = True

        if new_episode:
            flush()
            current_session = sid
            current_anchor = anchor

        if not current_anchor:
            current_anchor = anchor

        current_steps.append(
            Step(
                tool_name=row.get("tool_name"),
                tool_input=row.get("tool_input"),
                tool_response_preview=row.get("tool_response_preview"),
                tool_success=row.get("tool_success"),
                tool_error=row.get("tool_error"),
                created_at=row.get("created_at"),
            )
        )

    flush()
    return episodes


def _annotate(episodes: list[dict[str, Any]], profile: dict[str, Any], successful_only: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ep in episodes:
        steps = [
            Step(
                tool_name=s.get("tool_name"),
                tool_input=s.get("tool_input"),
                tool_response_preview=s.get("tool_response_preview"),
                tool_success=s.get("tool_success"),
                tool_error=s.get("tool_error"),
                created_at=None,
            )
            for s in ep["tools"]
        ]
        flags = _signal_flags(ep.get("prompt_text", ""), steps, profile)
        reward = _compute_reward(steps, flags, profile)

        if successful_only and any((s.tool_success is False) or s.tool_error for s in steps):
            continue

        out.append(
            {
                "prompt_text": ep.get("prompt_text", ""),
                "tools": ep["tools"],
                "signals": flags,
                "reward": reward,
                "tool_count": len(ep["tools"]),
            }
        )
    return out


async def run(args: argparse.Namespace) -> dict[str, Any]:
    profile = _load_profile(Path(args.profile))

    conn = await asyncpg.connect(settings.effective_database_url.replace("postgresql://", "postgres://", 1))
    try:
        rows = await _fetch_rows(
            conn,
            args.project,
            args.limit,
            args.offset,
            args.strict_project,
        )
    finally:
        await conn.close()

    episodes = _build_episodes(rows, args.min_steps, args.max_steps)
    annotated = _annotate(episodes, profile, args.successful_only)

    raw_out_dir = Path(args.raw_output_dir)
    proc_out_dir = Path(args.processed_output_dir)
    raw_out_dir.mkdir(parents=True, exist_ok=True)
    proc_out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    raw_file = raw_out_dir / f"rl_episodes_raw_{ts}.jsonl"
    proc_file = proc_out_dir / f"rl_episodes_scored_{ts}.jsonl"

    with raw_file.open("w", encoding="utf-8") as f:
        for ep in episodes:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")

    with proc_file.open("w", encoding="utf-8") as f:
        for ep in annotated:
            f.write(json.dumps(ep, ensure_ascii=False) + "\n")

    summary = {
        "rows_fetched": len(rows),
        "episodes_raw": len(episodes),
        "episodes_scored": len(annotated),
        "successful_only": args.successful_only,
        "raw_file": str(raw_file),
        "processed_file": str(proc_file),
        "project": args.project,
    }
    (proc_out_dir / f"rl_episodes_summary_{ts}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build scored RL episodes from tool-call history")
    p.add_argument("--project", default=None)
    p.add_argument(
        "--strict-project",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When true, require exact project_path or project_name match",
    )
    p.add_argument("--limit", type=int, default=12000)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--min-steps", type=int, default=2)
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--successful-only", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--profile", default="fine-tune/rl_reward_profile.json")
    p.add_argument("--raw-output-dir", default="data/raw/rl")
    p.add_argument("--processed-output-dir", default="data/processed/rl")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    summary = asyncio.run(run(args))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
