"""Verify live native prompt-to-tool linkage through HTTP and the DB ledger."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_live_codex_queue_links_prompt_only_in_same_session_and_project(
    client, test_project, test_prefix
):
    session = test_prefix + "-native-codex"
    prompt = await client.post(
        "/api/prompts",
        json={
            "session_id": session,
            "cwd": test_project,
            "agent_name": "codex-cli",
            "prompt": "Check this project before recording its tools",
        },
    )
    assert prompt.status_code == 200
    prompt_id = prompt.json()["id"]
    for name, cwd in [
        ("same-project", test_project),
        ("foreign-project", test_project + "-sibling"),
    ]:
        response = await client.post(
            "/api/queue",
            json={
                "session_id": session,
                "cwd": cwd,
                "tool_name": test_prefix + name,
                "tool_input": {},
                "tool_response_preview": "ok",
                "source_system": "codex-cli",
                "source_agent": "codex-cli",
                "source_mode": "hook",
            },
        )
        assert response.status_code == 200
    # Use the required wrapper for read-side DB verification. The generated
    # test marker is alphanumeric/hyphen and cannot contain SQL metacharacters.
    query = f"SELECT json_agg(x) FROM (SELECT tool_name,prev_user_prompt_id,prompt_text FROM mem_tool_calls WHERE tool_name IN ('{test_prefix}same-project','{test_prefix}foreign-project')) x;"
    proc = subprocess.run(
        [str(ROOT / "scripts/psql_wrapper.sh"), "-qAt", "-c", query],
        text=True,
        capture_output=True,
        check=True,
    )
    rows = {row["tool_name"]: row for row in json.loads(proc.stdout)}
    assert rows[test_prefix + "same-project"]["prev_user_prompt_id"] == prompt_id
    assert (
        rows[test_prefix + "same-project"]["prompt_text"]
        == "Check this project before recording its tools"
    )
    assert rows[test_prefix + "foreign-project"]["prev_user_prompt_id"] is None
