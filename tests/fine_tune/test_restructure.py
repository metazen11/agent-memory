"""Unit tests for fine-tune/restructure_to_qwen_tools.py

Pure-logic tests of PII scrubbing, schema inference, and tool-call parsing.
Run with:
    .venv-finetune/bin/python -m pytest tests/fine_tune/test_restructure.py -v
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fine-tune"))

from restructure_to_qwen_tools import (  # noqa: E402
    infer_tool_schemas,
    parse_assistant_tool_call,
    parse_tool_response,
    scrub_string,
    scrub_value,
)


# ---- PII scrub -------------------------------------------------------------

def test_scrub_sk_token():
    c = Counter()
    out = scrub_string("here is sk-AbcDefGhi1234567890_AbcDefGhi end", c)
    assert "sk-" not in out
    assert "<REDACTED_TOKEN>" in out
    assert c["sk_token"] == 1


def test_scrub_bearer():
    c = Counter()
    out = scrub_string("Authorization: Bearer abc123xyz", c)
    assert "Bearer abc123xyz" not in out
    assert c["bearer"] == 1


def test_scrub_agent_memory_token():
    c = Counter()
    out = scrub_string('AGENT_MEMORY_TOKEN="secret123"', c)
    assert "secret123" not in out
    assert c["agent_memory_token"] == 1


def test_scrub_home_path():
    c = Counter()
    out = scrub_string("Read /Users/mz/Dropbox/file.py", c)
    assert "/Users/mz/" not in out
    assert "/Users/<user>/Dropbox/file.py" in out
    assert c["user_path"] == 1


def test_scrub_multiple_in_one_string():
    c = Counter()
    out = scrub_string("call /Users/mz/x with Bearer xyz and sk-A1B2C3D4E5F6G7H8I9J0K1L2", c)
    assert c["user_path"] == 1
    assert c["bearer"] == 1
    assert c["sk_token"] == 1


def test_scrub_no_false_positive_on_short_sk():
    c = Counter()
    out = scrub_string("sk-short", c)  # < 20 chars, should NOT match
    assert out == "sk-short"
    assert c["sk_token"] == 0


def test_scrub_value_recurses_dicts_and_lists():
    c = Counter()
    nested = {"a": ["Bearer xyz", {"b": "/Users/mz/x"}], "c": 42}
    out = scrub_value(nested, c)
    assert "Bearer xyz" not in str(out)
    assert "/Users/mz/" not in str(out)
    assert out["c"] == 42  # non-string preserved
    assert c["bearer"] == 1
    assert c["user_path"] == 1


# ---- Tool-call parsing -----------------------------------------------------

def test_parse_assistant_tool_call_well_formed():
    content = '{"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response_preview": "..."}'
    out = parse_assistant_tool_call(content)
    assert out is not None
    assert out["tool_name"] == "Bash"
    assert out["tool_input"] == {"command": "ls"}


def test_parse_assistant_tool_call_malformed_json():
    out = parse_assistant_tool_call('{not json}')
    assert out is None


def test_parse_assistant_tool_call_empty_tool_name_returned_as_is():
    """Parser passes through empty tool_name; downstream restructure_row filters."""
    content = '{"tool_name": "", "tool_input": {}}'
    out = parse_assistant_tool_call(content)
    assert out is not None
    assert out["tool_name"] == ""


def test_parse_tool_response_string():
    assert parse_tool_response("hello") == "hello"


def test_parse_tool_response_dict_serializes():
    out = parse_tool_response({"stdout": "hi", "interrupted": False})
    # Must be a string (the role:tool message content must be str)
    assert isinstance(out, str)
    assert "stdout" in out


# ---- Schema inference ------------------------------------------------------

def test_infer_schema_required_at_threshold():
    """Field present in >= 95% of rows is required."""
    stats = {
        "MyTool": {
            "total": 100,
            "fields": {
                "always": {"count": 100, "types": {"string"}},
                "almost": {"count": 95, "types": {"string"}},
                "sometimes": {"count": 50, "types": {"string"}},
                "rarely": {"count": 5, "types": {"string"}},
            },
        }
    }
    schemas = infer_tool_schemas(stats)
    assert "MyTool" in schemas
    required = set(schemas["MyTool"]["required"])
    assert "always" in required
    assert "almost" in required  # at exactly 95% threshold
    assert "sometimes" not in required
    assert "rarely" not in required
    # All fields appear in properties regardless
    assert set(schemas["MyTool"]["properties"]) == {"always", "almost", "sometimes", "rarely"}


def test_infer_schema_below_threshold_not_required():
    stats = {
        "MyTool": {
            "total": 100,
            "fields": {"x": {"count": 94, "types": {"string"}}},  # below 95%
        }
    }
    schemas = infer_tool_schemas(stats)
    # required key is omitted (or empty) when no fields qualify
    required = schemas["MyTool"].get("required", [])
    assert required == []
