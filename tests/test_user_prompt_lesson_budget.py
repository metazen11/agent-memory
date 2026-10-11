"""formatLessons() must bound the injected lesson block (token budget)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "user-prompt-submit.js"

JS = """
const m = require(process.argv[1]);
const lessons = JSON.parse(process.argv[2]);
process.stdout.write(m.formatLessons(lessons));
"""


def _format(lessons: list[dict], env: dict | None = None) -> str:
    import os

    proc = subprocess.run(
        ["node", "-e", JS, str(HOOK), json.dumps(lessons)],
        text=True, capture_output=True, check=True,
        env={**os.environ, **(env or {})},
    )
    return proc.stdout


def _lesson(i: int, rule: str) -> dict:
    return {"id": i, "severity": "critical", "project_name": None, "rule": rule}


def test_long_rule_truncated_at_word_boundary() -> None:
    rule = ("word " * 600).strip()  # ~3KB
    out = _format([_lesson(1, rule)])
    assert "search_lessons for full text" in out
    assert len(out) < 600
    assert "wor …" not in out  # cut on a word boundary, not mid-word


def test_short_rule_passes_through_untouched() -> None:
    out = _format([_lesson(1, "Never force-push main.")])
    assert "Never force-push main." in out
    assert "search_lessons" not in out


def test_block_capped_at_6kb_for_twenty_huge_lessons() -> None:
    out = _format([_lesson(i, "x" * 3000 + " end") for i in range(1, 21)])
    assert len(out.encode()) <= 6144


def test_block_cap_overridable_via_env() -> None:
    lessons = [_lesson(i, "y" * 3000) for i in range(1, 21)]
    small = _format(lessons, {"AGENT_MEMORY_LESSON_BLOCK_MAX_BYTES": "1500"})
    assert len(small.encode()) <= 1500
    assert "more lesson(s) omitted" in small
    wide = _format(lessons, {"AGENT_MEMORY_LESSON_RULE_MAX_CHARS": "1000",
                             "AGENT_MEMORY_LESSON_BLOCK_MAX_BYTES": "100000"})
    assert len(wide.encode()) > 15000
