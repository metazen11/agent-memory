import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "codex_backfill", ROOT / "scripts/backfill/backfill_codex_prompts.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_parser_ignores_context_and_tracks_project_changes(tmp_path):
    file = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": "native",
                "cwd": "/Users/mz/Dropbox/_CODING/agentMemory",
                "source": "vscode",
            },
        },
        {
            "timestamp": "2026-10-03T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>context</environment_context>",
                    }
                ],
            },
        },
        {
            "timestamp": "2026-10-03T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fix the hooks"}],
            },
        },
        {"type": "turn_context", "payload": {"cwd": "/another/project"}},
        {
            "timestamp": "2026-10-03T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "yes"}],
            },
        },
        {
            "timestamp": "2026-10-03T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "yes"}],
            },
        },
    ]
    file.write_text("\n".join(map(json.dumps, records)) + "\nnot json\nnull\n")
    rows, counts = m.parse_transcript(file)
    assert [r["text"] for r in rows] == ["fix the hooks", "yes", "yes"]
    assert rows[0]["cwd"] == "/Users/mz/_CODING/agentMemory"
    assert rows[1]["cwd"] == "/another/project"
    assert counts["malformed"] == 2 and counts["context_or_empty"] == 1
    planned, skipped = m.plan(rows + rows, set())
    assert len(planned["native"]) == 3 and skipped == 3
    planned, skipped = m.plan(rows, {m.prompt_identity(rows[1])})
    assert len(planned["native"]) == 2 and skipped == 1
    assert planned["native"][-1]["ordinal"] == 3


def test_delegated_transcript_not_imported(tmp_path):
    file = tmp_path / "delegated.jsonl"
    file.write_text(
        "\n".join(
            map(
                json.dumps,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child",
                            "cwd": "/repo",
                            "source": {"subagent": "review"},
                        },
                    },
                    {
                        "timestamp": "2026-10-03T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Review the implementation",
                                }
                            ],
                        },
                    },
                ],
            )
        )
    )
    rows, counts = m.parse_transcript(file)
    assert rows == [] and counts["delegated"] == 1


def test_sql_literal_cannot_escape_string():
    assert m.literal("a'; DROP TABLE x; --") == "'a''; DROP TABLE x; --'"
    assert m.literal(None) == "NULL"


def test_repeated_prompt_matches_later_occurrence_not_anonymous_count():
    rows = [
        {
            "sid": "native",
            "cwd": "/repo",
            "text": "yes",
            "hash": "same",
            "timestamp": ts,
        }
        for ts in ["2026-10-03T00:00:01Z", "2026-10-03T00:00:03Z"]
    ]
    planned, skipped = m.plan(rows, {m.prompt_identity(rows[1])})
    assert skipped == 1
    assert planned["native"] == [rows[0] | {"ordinal": 1}]


def test_occurrences_keep_project_and_normalize_timestamp_identity():
    row = {
        "sid": "native",
        "cwd": "/repo",
        "text": "yes",
        "hash": "same",
        "timestamp": "2026-10-03T00:00:01Z",
    }
    other = row | {"cwd": "/other"}
    equivalent = row | {"timestamp": "2026-10-02T17:00:01-07:00"}
    planned, skipped = m.plan([row, other, equivalent], {m.prompt_identity(row)})
    assert skipped == 2
    assert planned["native"] == [other | {"ordinal": 2}]


def test_insert_guard_includes_occurrence_project(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "psql", lambda sql: calls.append(sql) or "1")
    row = {
        "sid": "native",
        "cwd": "/repo",
        "text": "yes",
        "hash": "same",
        "timestamp": "2026-10-03T00:00:01Z",
        "ordinal": 1,
    }
    assert (
        m.write_session("native", [row], "test", {"/repo": ("/repo", None, "non-git")})
        == 1
    )
    assert (
        "p.project_id=(SELECT id FROM mem_projects WHERE full_path=c.project_path)"
        in calls[0]
    )
