"""Tests for automatic lesson distillation.

The highest-value assertions here are the NEGATIVE ones. A distiller that
creates bad lessons is worse than none: every lesson it writes is injected
into future sessions, so a vague or broad-match rule permanently degrades
every session it touches.
"""

from __future__ import annotations

import pytest

from app.lesson_distill import (
    build_trigger,
    is_noise,
    normalize_error,
    validate_lesson_payload,
    _extract_files,
    _extract_pattern,
)


# ── Normalization / clustering ────────────────────────────────

def test_normalize_collapses_digit_variants():
    """`matches 138 times` and `matches 192 times` must cluster together.

    Before normalization these were two clusters of 578 and 296; merging
    them yields one 892-count cluster, which is what earns a lesson.
    """
    a = normalize_error("Error: old_string matches 138 times — provide more context")
    b = normalize_error("Error: old_string matches 192 times — provide more context")
    assert a == b


def test_normalize_masks_hashes_and_timestamps():
    a = normalize_error("failed at 2026-09-23T10:11:12Z commit deadbeef1234567")
    assert "2026-09-23" not in a
    assert "deadbeef1234567" not in a


def test_noise_filter_rejects_uninformative_errors():
    assert is_noise(normalize_error("Command failed (exit code 1, no output)"))
    assert is_noise("")
    assert is_noise("short")


def test_noise_filter_keeps_real_failures():
    real = normalize_error("fatal: 'public_html/.git' not recognized as a git repository")
    assert not is_noise(real)


# ── Trigger construction ──────────────────────────────────────

def _candidate(**over):
    base = {
        "tool_name": "bash_run",
        "normalized_error": "fatal: 'public_html/.git' not recognized as a git repository",
        "sample_errors": ["fatal: 'public_html/.git' not recognized as a git repository"],
        "sample_inputs": [],
    }
    base.update(over)
    return base


def test_trigger_is_never_broad_match():
    """Every generated trigger must be narrow.

    trigger_on='input' with neither tool nor pattern matches every
    Edit/Write/Bash call and dominates the systemMessage budget. Migration
    016 refuses such rows at the DB; this asserts we never even try.
    """
    cases = [
        _candidate(),
        _candidate(normalized_error="Error: command timed out after Ns", sample_errors=["timed out"]),
        _candidate(tool_name=None, normalized_error="x" * 50, sample_errors=["x" * 50]),
    ]
    for cand in cases:
        trigger = build_trigger(cand)
        if trigger.get("_unusable"):
            # Explicitly refused — the orchestrator drops these instead of
            # attempting an insert the DB would reject. That is a pass.
            continue
        if trigger["trigger_on"] == "input":
            assert trigger.get("trigger_tool") or trigger.get("trigger_pattern"), (
                f"broad-match trigger generated for {cand['normalized_error'][:40]}"
            )
        elif trigger["trigger_on"] == "file_scope":
            assert trigger.get("trigger_files")


def test_file_trigger_requires_error_to_name_the_file():
    """A file trigger is only valid when the failure is ABOUT that file.

    Regression: the generic "old_string matches N times" cluster was being
    scoped to .mcp.json purely because that file was open at the time. The
    real lesson applies to every file, so the file trigger was wrong.
    """
    cand = _candidate(
        tool_name="edit_file",
        normalized_error="Error: old_string matches N times — provide more context",
        sample_errors=["Error: old_string matches 138 times — provide more context"],
        sample_inputs=['{"file_path": "/repo/.mcp.json"}', '{"file_path": "/repo/.mcp.json"}'],
    )
    assert _extract_files(cand) == [], "file not named in the error must not become a trigger"


def test_file_trigger_used_when_error_names_the_file():
    cand = _candidate(
        tool_name="edit_file",
        normalized_error="Error: old_string not found in .mcp.json. Re-read the file",
        sample_errors=["Error: old_string not found in .mcp.json. Re-read the file"] * 2,
        sample_inputs=['{"file_path": "/repo/.mcp.json"}', '{"file_path": "/repo/.mcp.json"}'],
    )
    assert ".mcp.json" in _extract_files(cand)


def test_pattern_rejects_generic_tokens():
    """`bin/sh` would fire on nearly every shell command."""
    cand = _candidate(
        normalized_error="/bin/sh: wp: command not found",
        sample_errors=["/bin/sh: wp: command not found"],
    )
    pattern = _extract_pattern(cand)
    assert pattern is None or "bin/sh" not in pattern


def test_pattern_rejects_normalization_placeholders():
    """`N.N/N.N.N` is our own masking artifact and matches nothing real."""
    cand = _candidate(
        normalized_error="/opt/homebrew/Cellar/python@N.N/N.N.N/Frameworks/Python.framework",
        sample_errors=["/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks"],
    )
    pattern = _extract_pattern(cand) or ""
    assert "N.N" not in pattern.replace("\\", "")


def test_pattern_is_valid_regex_and_bounded():
    import re
    cand = _candidate()
    pattern = _extract_pattern(cand)
    if pattern:
        re.compile(pattern)
        assert len(pattern) <= 200


# ── Validation gate ───────────────────────────────────────────

_VALID_TRIGGER = {"trigger_on": "input", "trigger_tool": "bash_run"}


def test_validator_rejects_thin_rule():
    """The local 7B emits one-clause restatements that teach nothing.

    "Set replace_all=True in edit_file calls" is also WRONG advice — blind
    replace_all makes incorrect edits. Length alone is the cheap signal
    that catches this class.
    """
    ok, reason = validate_lesson_payload({
        "title": "Use replace_all",
        "rule": "Set replace_all=True in edit_file calls.",
        "severity": "warning",
        **_VALID_TRIGGER,
    })
    assert not ok and "short" in reason


def test_validator_rejects_vague_rule():
    ok, reason = validate_lesson_payload({
        "title": "Careful",
        "rule": "Be careful when editing configuration files because mistakes can cause problems later.",
        "severity": "warning",
        **_VALID_TRIGGER,
    })
    assert not ok and "vague" in reason


def test_validator_rejects_broad_match_input_trigger():
    ok, reason = validate_lesson_payload({
        "title": "Something",
        "rule": "Run `git -C public_html rev-parse --git-dir` before git commands in that directory.",
        "severity": "warning",
        "trigger_on": "input",
    })
    assert not ok and "broad-match" in reason


def test_validator_rejects_invalid_regex():
    ok, reason = validate_lesson_payload({
        "title": "Bad regex",
        "rule": "Run `git -C public_html rev-parse --git-dir` before git commands in that directory.",
        "severity": "warning",
        "trigger_on": "input",
        "trigger_tool": "bash_run",
        "trigger_pattern": "unclosed(",
    })
    assert not ok and "regex" in reason


def test_validator_rejects_bad_severity():
    ok, reason = validate_lesson_payload({
        "title": "T",
        "rule": "Run `git -C public_html rev-parse --git-dir` before git commands in that directory.",
        "severity": "urgent",
        **_VALID_TRIGGER,
    })
    assert not ok and "severity" in reason


def test_validator_accepts_a_good_lesson():
    ok, reason = validate_lesson_payload({
        "title": "Check git repository path",
        "rule": (
            "Run `git -C public_html rev-parse --git-dir` before any git command "
            "targeting public_html. It is a plain directory in this project, not a "
            "repository, so `git -C public_html status` fails."
        ),
        "severity": "warning",
        "trigger_on": "input",
        "trigger_tool": "bash_run",
        "trigger_pattern": r"public_html/\.git",
    })
    assert ok, reason


def test_untriggerable_candidate_is_refused_not_emitted():
    """With no tool and no distinctive token, the candidate must be dropped.

    Emitting it would mean a broad-match lesson firing on every tool call.
    """
    cand = _candidate(tool_name=None, normalized_error="x" * 50, sample_errors=["x" * 50])
    assert build_trigger(cand).get("_unusable") is True


# ── Duplicate detection ───────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_detection_catches_resynthesized_lesson():
    """A re-run must not create a second copy of the same lesson.

    Regression: the pre-synthesis probe embeds ERROR TEXT while lessons
    store `title\\nrule`. Comparing across those vocabularies scored an
    exact duplicate at only 0.710 — below any threshold that would not
    also swallow an unrelated lesson at 0.644 — so a scheduled re-run
    created a byte-identical duplicate. find_duplicate_lesson() compares
    like with like and scores the same pair at 1.00.
    """
    import httpx
    from app.db import init_pool, get_pool
    from app.lesson_distill import find_duplicate_lesson

    title = "Distill dedup fixture lesson"
    rule = (
        "Run `git -C public_html rev-parse --git-dir` before any git command "
        "targeting public_html; it is a plain directory, not a repository."
    )

    async with httpx.AsyncClient(
        base_url="http://localhost:3377", timeout=10.0,
        headers={"X-Agent-Name": "claude"},
    ) as client:
        resp = await client.post("/api/lessons", json={
            "title": title,
            "rule": rule,
            "severity": "warning",
            "trigger_on": "input",
            "trigger_tool": "bash_run",
        })
        if resp.status_code == 429:
            # Shared localhost write bucket (require_auth=False keys it on
            # client host, so tests, CLI runs and live hooks all compete).
            pytest.skip("shared localhost write budget exhausted; re-run when idle")
        resp.raise_for_status()
        lesson_id = resp.json()["id"]

        try:
            await init_pool()
            pool = await get_pool()
            async with pool.acquire() as conn:
                dup = await find_duplicate_lesson(conn, title, rule)
            assert dup is not None, "identical lesson was not detected as duplicate"
            assert dup["id"] == lesson_id
            assert dup["similarity"] >= 0.90
        finally:
            await client.patch(f"/api/lessons/{lesson_id}", json={"active": False})
