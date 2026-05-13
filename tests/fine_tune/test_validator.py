"""Unit tests for the tool-call validator parser.

Pure-logic tests — no model required. Run with:
    .venv-finetune/bin/python -m pytest tests/fine_tune/test_validator.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fine_tune"))

from validate_tool_calls import parse_tool_calls  # noqa: E402

# Pinned test schemas — independent of whatever the trained-tools registry has,
# so the unit tests stay stable when the dataset changes.
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "get_weather", "description": "Weather.",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
                       "required": ["city"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run_bash", "description": "Run bash.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]


def test_parses_well_formed_single_tool_call():
    text = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "SF"}}\n</tool_call>'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is True
    assert r.tool_calls[0]["name"] == "get_weather"
    assert r.tool_calls[0]["arguments"] == {"city": "SF"}
    assert r.schema_valid is True


def test_parses_with_extra_whitespace_and_prose():
    text = (
        "Let me check that for you.\n\n"
        '<tool_call>\n   {"name": "get_weather", "arguments": {"city": "Tokyo", "unit": "celsius"}}   \n</tool_call>\n'
        "I'll have an answer in a moment."
    )
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is True
    assert r.schema_valid is True


def test_parses_multiple_tool_calls():
    text = (
        '<tool_call>{"name": "get_weather", "arguments": {"city": "A"}}</tool_call>\n'
        '<tool_call>{"name": "read_file", "arguments": {"path": "/etc/hosts"}}</tool_call>'
    )
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is True
    assert len(r.tool_calls) == 2


def test_rejects_missing_tags():
    text = '{"name": "get_weather", "arguments": {"city": "SF"}}'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is False
    assert "no <tool_call> tags" in r.error


def test_rejects_malformed_json():
    text = '<tool_call>{"name": "get_weather", arguments: {city: SF}}</tool_call>'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is False
    assert "json" in r.error.lower()


def test_rejects_missing_required_field():
    """Schema requires 'city' for get_weather — empty args must fail validation."""
    text = '<tool_call>{"name": "get_weather", "arguments": {}}</tool_call>'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is True
    assert r.schema_valid is False
    assert "get_weather" in r.error


def test_rejects_unknown_tool():
    text = '<tool_call>{"name": "delete_everything", "arguments": {}}</tool_call>'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is True
    assert r.schema_valid is False
    assert "delete_everything" in r.error


def test_rejects_missing_name_or_arguments():
    text = '<tool_call>{"name": "get_weather"}</tool_call>'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is False
    assert "missing" in r.error.lower()


def test_handles_nested_quotes_in_arguments():
    """Tool args often contain shell commands with quotes — must round-trip cleanly."""
    text = '<tool_call>{"name": "run_bash", "arguments": {"command": "echo \\"hi\\""}}</tool_call>'
    r = parse_tool_calls(text, TOOL_SCHEMAS)
    assert r.parsed is True
    assert r.tool_calls[0]["arguments"]["command"] == 'echo "hi"'


def test_empty_string():
    r = parse_tool_calls("", TOOL_SCHEMAS)
    assert r.parsed is False
