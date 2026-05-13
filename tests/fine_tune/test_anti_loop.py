"""Unit tests for the anti-loop guard.

The empty-args infinite-loop bug in v1: model emits `<tool_call>` with empty
`arguments`, gets a generic tool result, emits the same call again, and
again. AntiLoopDetector watches a conversation's tool-call stream and flags
the 3rd consecutive identical call for suppression.

Run:
    .venv-finetune/bin/python -m pytest tests/fine_tune/test_anti_loop.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fine_tune"))

from validate_tool_calls import AntiLoopDetector, _normalize_call  # noqa: E402


# ---- The canonical loop-bug fixture ----------------------------------------

LOOP_CONVERSATION = [
    {"name": "Bash", "arguments": {}},
    {"name": "Bash", "arguments": {}},
    {"name": "Bash", "arguments": {}},
    {"name": "Bash", "arguments": {}},
]


def test_third_consecutive_identical_call_is_suppressed():
    det = AntiLoopDetector()
    decisions = [det.observe(c) for c in LOOP_CONVERSATION]
    assert [d.suppress for d in decisions] == [False, False, True, True]
    assert det.suppressions == 2


def test_two_consecutive_then_different_does_not_trigger():
    det = AntiLoopDetector()
    seq = [
        {"name": "Bash", "arguments": {"command": "ls"}},
        {"name": "Bash", "arguments": {"command": "ls"}},
        {"name": "Read", "arguments": {"path": "/tmp"}},
        {"name": "Bash", "arguments": {"command": "ls"}},
    ]
    decisions = [det.observe(c) for c in seq]
    assert not any(d.suppress for d in decisions)
    assert det.suppressions == 0


def test_filter_sequence_removes_suppressed_calls():
    det = AntiLoopDetector()
    surviving = det.filter_sequence(LOOP_CONVERSATION)
    assert len(surviving) == 2
    assert det.suppressions == 2


def test_empty_args_counter_increments_regardless_of_suppression():
    det = AntiLoopDetector()
    det.observe({"name": "Bash", "arguments": {}})
    det.observe({"name": "Read", "arguments": {}})
    det.observe({"name": "Write", "arguments": {"path": "/a", "content": "b"}})
    assert det.empty_args_emissions == 2


def test_arguments_compared_by_normalized_form_not_key_order():
    det = AntiLoopDetector()
    a = {"name": "Bash", "arguments": {"command": "ls", "description": "list"}}
    b = {"name": "Bash", "arguments": {"description": "list", "command": "ls"}}
    c = {"name": "Bash", "arguments": {"description": "list", "command": "ls"}}
    decisions = [det.observe(x) for x in (a, b, c)]
    assert decisions[2].suppress is True


def test_different_arguments_reset_the_streak():
    det = AntiLoopDetector()
    seq = [
        {"name": "Bash", "arguments": {"command": "ls"}},
        {"name": "Bash", "arguments": {"command": "ls"}},
        {"name": "Bash", "arguments": {"command": "pwd"}},
        {"name": "Bash", "arguments": {"command": "pwd"}},
    ]
    decisions = [det.observe(c) for c in seq]
    assert not any(d.suppress for d in decisions)


def test_threshold_below_two_is_rejected():
    with pytest.raises(ValueError):
        AntiLoopDetector(threshold=1)


def test_normalize_call_is_stable_across_key_order():
    a = _normalize_call({"name": "X", "arguments": {"a": 1, "b": 2}})
    b = _normalize_call({"name": "X", "arguments": {"b": 2, "a": 1}})
    assert a == b


def test_model_version_propagates_to_warn_log(caplog):
    det = AntiLoopDetector(model_version="v1")
    with caplog.at_level("WARNING"):
        for call in LOOP_CONVERSATION:
            det.observe(call)
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("model=v1" in r.getMessage() for r in warn_records)
    assert det.model_version == "v1"


def test_custom_threshold_changes_suppression_point():
    det = AntiLoopDetector(threshold=2)
    decisions = [det.observe(c) for c in LOOP_CONVERSATION]
    assert decisions[0].suppress is False
    assert decisions[1].suppress is True
    assert det.suppressions == 3
