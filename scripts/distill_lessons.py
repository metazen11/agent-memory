#!/usr/bin/env python
"""Mine recurring failures into lessons.

Run manually, or on a schedule via launchd (see
scripts/install_distill_schedule.sh).

    # See what it WOULD create (safe, default):
    .venv/bin/python scripts/distill_lessons.py

    # Actually create them:
    .venv/bin/python scripts/distill_lessons.py --apply

Writing is opt-in: a created lesson is injected into every matching future
session, so --apply must be passed explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import init_pool, get_pool, close_pool  # noqa: E402
from app.lesson_distill import distill_once  # noqa: E402


def _print_report(report: dict) -> None:
    mode = "DRY RUN" if report["dry_run"] else "APPLIED"
    print(f"\n=== lesson distillation [{mode}] ===")
    print(f"candidates examined : {report['candidates_examined']}")
    print(f"proposed            : {len(report['proposed'])}")
    print(f"created             : {len(report['created'])}")
    print(f"rejected            : {len(report['rejected'])}")

    for lesson in report["proposed"]:
        evidence = lesson.get("_evidence") or {}
        print(f"\n--- {lesson['title']}  [{lesson['severity']}] ---")
        if evidence:
            print(f"  evidence : {evidence.get('occurrences')}x across "
                  f"{evidence.get('sessions')} session(s), last "
                  f"{evidence.get('last_seen')}")
        triggers = {k: v for k, v in lesson.items() if k.startswith("trigger")}
        print(f"  trigger  : {triggers}")
        print(f"  project  : {lesson.get('project')}")
        print(f"  rule     : {lesson['rule']}")

    for created in report["created"]:
        print(f"\nCREATED #{created['id']}: {created['title']}")

    for rejected in report["rejected"]:
        print(f"\nREJECTED ({rejected['reason']}): {rejected['error'][:70]}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually create lessons (default: dry run)")
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--min-occurrences", type=int, default=10,
                        help="how often a failure must recur to qualify")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--quiet", action="store_true", help="errors only")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.ERROR if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    await init_pool()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            report = await distill_once(
                conn,
                lookback_days=args.lookback_days,
                min_occurrences=args.min_occurrences,
                limit=args.limit,
                dry_run=not args.apply,
            )
    finally:
        await close_pool()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
