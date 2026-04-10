#!/usr/bin/env python3
"""Export training datasets directly from the agentMemory PostgreSQL DB.

Writes raw export files under data/raw/ by default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.dataset_exports import build_dataset_records, fetch_tool_call_rows


def _is_path_like(project: str) -> bool:
    return "/" in project or project.startswith(".")


def _row_matches_exact_project(row: dict, project: str) -> bool:
    project_path = str(row.get("project_path") or "")
    project_name = str(row.get("project_name") or "")
    if _is_path_like(project):
        return project_path == project
    return project_name == project


def _slugify(text: str) -> str:
    safe = []
    for ch in text:
        if ch.isalnum():
            safe.append(ch.lower())
        elif ch in {"/", "\\", "-", "_", "."}:
            safe.append("-")
    slug = "".join(safe).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:64] or "global"


async def _export(args: argparse.Namespace) -> tuple[Path, int]:
    dsn = settings.effective_database_url
    conn = await asyncpg.connect(dsn.replace("postgresql://", "postgres://", 1))
    try:
        rows = await fetch_tool_call_rows(
            conn,
            project=args.project,
            limit=args.limit,
            offset=args.offset,
        )
    finally:
        await conn.close()

    if args.project and args.strict_project:
        rows = [r for r in rows if _row_matches_exact_project(r, args.project)]

    records = build_dataset_records(
        rows,
        dataset_type=args.dataset_type,
        include_errors=args.include_errors,
        include_observations=args.include_observations,
        min_reward=args.min_reward,
        max_reward=args.max_reward,
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = _slugify(args.project) if args.project else "global"
    out_file = out_dir / f"{args.dataset_type}_{scope}_{ts}.jsonl"

    with out_file.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_type": args.dataset_type,
        "project": args.project,
        "include_errors": args.include_errors,
        "include_observations": args.include_observations,
        "min_reward": args.min_reward,
        "max_reward": args.max_reward,
        "limit": args.limit,
        "offset": args.offset,
        "row_count": len(rows),
        "record_count": len(records),
        "output_file": str(out_file),
    }
    with (out_dir / f"{args.dataset_type}_{scope}_{ts}.manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return out_file, len(records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export fine-tuning dataset from agentMemory DB")
    parser.add_argument("--dataset-type", choices=["sft", "trajectory", "preference"], default="sft")
    parser.add_argument("--project", default=None, help="Project full path or short name")
    parser.add_argument("--include-errors", action="store_true", help="Include problematic/error traces")
    parser.add_argument(
        "--include-observations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include linked observation payloads when available",
    )
    parser.add_argument("--min-reward", type=float, default=None)
    parser.add_argument("--max-reward", type=float, default=None)
    parser.add_argument("--limit", type=int, default=4000)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--output-dir", default="data/raw/agent_memory")
    parser.add_argument(
        "--strict-project",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When true, require exact project_path or project_name match",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_file, count = asyncio.run(_export(args))
    print(f"exported {count} records -> {out_file}")


if __name__ == "__main__":
    main()
