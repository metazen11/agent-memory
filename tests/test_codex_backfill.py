import importlib.util
import json
from collections import Counter
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
    planned, skipped = m.plan(rows + rows, {})
    assert len(planned["native"]) == 3 and skipped == 3
    repeated_hash = rows[1]["hash"]
    planned, skipped = m.plan(rows, {"native": Counter({repeated_hash: 1})})
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
