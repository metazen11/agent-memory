"""Unit tests for app.models — no DB or server required."""

import pytest

from app.models import (
    OBSERVATION_TYPES,
    normalize_observation_type,
    ObservationCreate,
    QueueItem,
    LessonCreate,
    SessionCreate,
)


# ── normalize_observation_type ─────────────────────────────────


class TestNormalizeObservationType:
    """Tests for the type normalization function."""

    @pytest.mark.parametrize("valid_type", OBSERVATION_TYPES)
    def test_valid_types_pass_through(self, valid_type):
        assert normalize_observation_type(valid_type) == valid_type

    @pytest.mark.parametrize("raw,expected", [
        ("fix", "bugfix"),
        ("bug", "bugfix"),
        ("bug-fix", "bugfix"),
        ("debug", "bugfix"),
        ("new", "feature"),
        ("add", "feature"),
        ("addition", "feature"),
        ("update", "change"),
        ("modify", "change"),
        ("modification", "change"),
        ("rename", "refactor"),
        ("cleanup", "refactor"),
        ("clean-up", "refactor"),
        ("restructure", "refactor"),
        ("finding", "discovery"),
        ("learn", "discovery"),
        ("insight", "discovery"),
        ("warning", "gotcha"),
        ("caveat", "gotcha"),
        ("pitfall", "gotcha"),
        ("convention", "pattern"),
        ("rule", "pattern"),
        ("choice", "decision"),
    ])
    def test_aliases_map_correctly(self, raw, expected):
        assert normalize_observation_type(raw) == expected

    @pytest.mark.parametrize("raw", ["xyz", "banana", "improvement", "other"])
    def test_unknown_falls_back_to_discovery(self, raw):
        assert normalize_observation_type(raw) == "discovery"

    def test_empty_string_falls_back(self):
        assert normalize_observation_type("") == "discovery"

    def test_whitespace_only_falls_back(self):
        assert normalize_observation_type("   ") == "discovery"

    @pytest.mark.parametrize("raw,expected", [
        ("Fix", "bugfix"),
        ("FIX", "bugfix"),
        ("BUGFIX", "bugfix"),
        ("Discovery", "discovery"),
        ("CHANGE", "change"),
    ])
    def test_case_insensitive(self, raw, expected):
        assert normalize_observation_type(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        (" fix ", "bugfix"),
        ("  bugfix", "bugfix"),
        ("decision  ", "decision"),
    ])
    def test_strips_whitespace(self, raw, expected):
        assert normalize_observation_type(raw) == expected

    def test_result_always_in_valid_set(self):
        """Fuzz-like: any input must produce a valid type."""
        for raw in ["", "fix", "XYZ", "   ", "bug-fix", "PATTERN", "unknown123"]:
            result = normalize_observation_type(raw)
            assert result in OBSERVATION_TYPES, f"'{raw}' → '{result}' not in OBSERVATION_TYPES"


# ── Pydantic models ────────────────────────────────────────────


class TestObservationCreate:
    def test_defaults(self):
        obs = ObservationCreate(session_id="s1", project="/tmp/p", title="Test")
        assert obs.type == "discovery"
        assert obs.facts == []
        assert obs.concepts == []
        assert obs.files_read == []
        assert obs.files_modified == []

    def test_all_fields(self):
        obs = ObservationCreate(
            session_id="s1",
            project="/tmp/p",
            title="Found a bug",
            subtitle="in auth",
            type="bugfix",
            narrative="Fixed JWT refresh",
            facts=["fact1"],
            concepts=["auth"],
            files_read=["/src/auth.py"],
            files_modified=["/src/auth.py"],
            tool_name="Edit",
        )
        assert obs.title == "Found a bug"
        assert obs.tool_name == "Edit"


class TestQueueItem:
    def test_minimal(self):
        item = QueueItem(session_id="s1")
        assert item.session_id == "s1"
        assert item.tool_name is None
        assert item.tool_input is None

    def test_full(self):
        item = QueueItem(
            session_id="s1",
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_response_preview="file1\nfile2",
            cwd="/tmp",
            last_user_message="list files",
        )
        assert item.tool_name == "Bash"
        assert item.tool_input["command"] == "ls"


class TestLessonCreate:
    def test_minimal(self):
        lesson = LessonCreate(title="Don't do X", rule="Always check Y before X")
        assert lesson.severity == "warning"
        assert lesson.project is None
        assert lesson.trigger_tool is None

    def test_full(self):
        lesson = LessonCreate(
            title="Check configs",
            rule="ALWAYS diff config before deploy",
            severity="critical",
            project="/home/user/app",
            trigger_tool="Bash",
            trigger_pattern="deploy.*prod",
        )
        assert lesson.severity == "critical"
        assert lesson.trigger_pattern == "deploy.*prod"


class TestSessionCreate:
    def test_defaults(self):
        s = SessionCreate(session_id="s1", project="/tmp/p")
        assert s.agent_type == "claude-code"
        assert s.project_path is None
