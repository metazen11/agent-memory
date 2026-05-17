"""Pure-logic tests for the v2 dataset builder.

These tests don't talk to the DB — they exercise the row-shaping,
filtering, and capping logic against synthetic inputs so the script's
behaviour is locked in regardless of what the live DB currently holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fine_tune"))

# Import the module under test. asyncpg is imported eagerly inside it,
# so if the test env doesn't have it we skip cleanly.
asyncpg = pytest.importorskip("asyncpg")

import build_v2_dataset as b  # noqa: E402


# ── _bash_command_token ─────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,expected", [
    ("git status",                                  "git"),
    ("git -C /repo log --oneline -5",               "git"),
    ("cd /tmp && git status",                       "git"),
    ("cd /a/b && cd /c/d && pytest tests/",         "cd"),   # nested cd; only one strip
    ("env CGO_ENABLED=0 go build ./...",            "go"),
    ("PYTHONPATH=. python -m pytest",               "python"),
    ("pytest -q",                                    "pytest"),
    ("",                                             "unknown"),
    (None,                                           "unknown"),
    ("|||",                                          "unknown"),
])
def test_bash_command_token(cmd, expected):
    assert b._bash_command_token(cmd) == expected


# ── _parse_tool_input ───────────────────────────────────────────────────

def test_parse_tool_input_handles_dict():
    assert b._parse_tool_input({"a": 1}) == {"a": 1}


def test_parse_tool_input_handles_json_string():
    assert b._parse_tool_input('{"a": 1}') == {"a": 1}


def test_parse_tool_input_returns_empty_on_garbage():
    assert b._parse_tool_input("not json") == {}
    assert b._parse_tool_input(None) == {}
    assert b._parse_tool_input(42) == {}


# ── _is_empty_args_problematic ─────────────────────────────────────────

def test_empty_args_problematic_when_required_fields_exist():
    schemas = {"Bash": {"required": ["command"]}}
    assert b._is_empty_args_problematic("Bash", {}, schemas) is True


def test_empty_args_OK_when_schema_allows_empty():
    schemas = {"NoArgsTool": {"required": []}}
    assert b._is_empty_args_problematic("NoArgsTool", {}, schemas) is False


def test_args_present_is_never_problematic():
    schemas = {"Bash": {"required": ["command"]}}
    assert b._is_empty_args_problematic("Bash", {"command": "ls"}, schemas) is False


def test_missing_schema_returns_false_so_other_drop_path_fires():
    # Missing schema → row drops via _build_tool_envelope returning None;
    # _is_empty_args_problematic should NOT double-flag it.
    assert b._is_empty_args_problematic("UnknownTool", {}, {}) is False


# ── _build_tool_envelope ───────────────────────────────────────────────

def test_envelope_has_function_shape():
    schemas = {"Bash": {"properties": {"command": {"type": "string"}}, "required": ["command"]}}
    env = b._build_tool_envelope("Bash", schemas)
    assert env is not None
    assert env["type"] == "function"
    assert env["function"]["name"] == "Bash"
    assert env["function"]["parameters"]["required"] == ["command"]
    assert env["function"]["description"]


def test_envelope_returns_none_for_unknown_tool():
    assert b._build_tool_envelope("UnknownTool", {}) is None


# ── _make_row ──────────────────────────────────────────────────────────

def _fake_record(**overrides) -> dict:
    """Return a dict shaped like an asyncpg.Record (good enough for _make_row)."""
    base = {
        "tool_call_id": 1,
        "tool_name": "Bash",
        "tool_input": '{"command": "ls -la"}',
        "tool_response_preview": "out",
        "turn_index": 1,
        "turn_subindex": 0,
        "prev_user_prompt_id": 1,
        "session_db_id": 1,
        "session_uuid": "deadbeef-1234",
        "prompt_text": "list files",
        "prompt_number": 1,
        "canonical_root_path": "/repo",
        "git_remote": "git@github.com:me/r.git",
        "source_kind": "git",
    }
    base.update(overrides)
    return base


SCHEMAS = {
    "Bash": {
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
    "Read": {
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    },
}


def test_make_row_basic_shape():
    rec = _fake_record()
    row = b._make_row(rec, SCHEMAS)
    assert row is not None
    msgs = row["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "list files"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "Bash"
    assert msgs[3]["role"] == "tool"
    assert row["source"] == "claude_jsonl"
    assert row["session_id"] == "deadbeef-1234"
    assert row["synthetic"] is False
    assert row["bash_command"] == "ls"


def test_make_row_drops_skip_tool():
    rec = _fake_record(tool_name="TodoWrite")
    assert b._make_row(rec, SCHEMAS) is None


def test_make_row_drops_unknown_tool():
    rec = _fake_record(tool_name="NotInRegistry")
    assert b._make_row(rec, SCHEMAS) is None


def test_make_row_drops_empty_args_against_required():
    rec = _fake_record(tool_input='{}')
    assert b._make_row(rec, SCHEMAS) is None


def test_make_row_drops_empty_prompt():
    rec = _fake_record(prompt_text="   ")
    assert b._make_row(rec, SCHEMAS) is None


def test_make_row_non_bash_has_no_bash_command():
    rec = _fake_record(tool_name="Read", tool_input='{"file_path": "/x"}')
    row = b._make_row(rec, SCHEMAS)
    assert row is not None
    assert "bash_command" not in row


# ── _apply_caps ────────────────────────────────────────────────────────

def _row_with_tool(tool_name: str, bash_command: str | None = None) -> dict:
    row = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "tool_calls": [
                {"type": "function",
                 "function": {"name": tool_name, "arguments": {}}}
            ]},
            {"role": "tool", "name": tool_name, "content": "r"},
        ],
        "tools": [],
        "source": "claude_jsonl",
        "session_id": "s1",
        "synthetic": False,
    }
    if bash_command:
        row["bash_command"] = bash_command
    return row


def test_apply_caps_passes_under_cap_through():
    # Cap = 20% of 10 = 2. Both categories already over cap (5 each),
    # both get capped to 2 → 4 total.
    rows = [_row_with_tool("Read") for _ in range(5)] + \
           [_row_with_tool("Edit") for _ in range(5)]
    out = b._apply_caps(rows)
    # cap = max(1, int(10 * 0.20)) = 2; each category trimmed to 2.
    read_count = sum(1 for r in out if r["messages"][2]["tool_calls"][0]["function"]["name"] == "Read")
    edit_count = sum(1 for r in out if r["messages"][2]["tool_calls"][0]["function"]["name"] == "Edit")
    assert read_count == 2
    assert edit_count == 2
    assert len(out) == 4


def test_apply_caps_trims_dominant_category():
    # 20% cap on 100 total = 20 per category. Read has 70, Edit has 30.
    # Both get trimmed to 20 → 40 total.
    rows = [_row_with_tool("Read") for _ in range(70)] + \
           [_row_with_tool("Edit") for _ in range(30)]
    out = b._apply_caps(rows)
    read_count = sum(
        1 for r in out
        if r["messages"][2]["tool_calls"][0]["function"]["name"] == "Read"
    )
    edit_count = sum(
        1 for r in out
        if r["messages"][2]["tool_calls"][0]["function"]["name"] == "Edit"
    )
    assert read_count == 20
    assert edit_count == 20
    assert len(out) == 40


def test_apply_caps_groups_bash_by_subcommand():
    # 100 total: 60 Bash:git, 20 Bash:pytest, 20 Read.
    # 20% cap = 20 per category. Bash:git capped to 20; others passthrough.
    rows = (
        [_row_with_tool("Bash", "git") for _ in range(60)] +
        [_row_with_tool("Bash", "pytest") for _ in range(20)] +
        [_row_with_tool("Read") for _ in range(20)]
    )
    out = b._apply_caps(rows)
    git_count = sum(1 for r in out if r.get("bash_command") == "git")
    pytest_count = sum(1 for r in out if r.get("bash_command") == "pytest")
    read_count = sum(
        1 for r in out
        if r["messages"][2]["tool_calls"][0]["function"]["name"] == "Read"
    )
    assert git_count == 20
    assert pytest_count == 20
    assert read_count == 20
    assert len(out) == 60
