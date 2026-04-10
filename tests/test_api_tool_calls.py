"""Integration tests for /api/tool-calls endpoints."""

import pytest


async def _queue_call(client, session_id: str, project: str, *, prompt: str, ok: bool) -> None:
    output = "all good" if ok else "Error: command not found"
    await client.post(
        "/api/queue",
        json={
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo ok" if ok else "badcmd"},
            "tool_response_preview": output,
            "cwd": project,
            "last_user_message": prompt,
            "source_system": "tests",
            "source_mode": "integration",
            "source_agent": "pytest",
        },
    )


@pytest.mark.asyncio
async def test_tool_calls_lookup(client, test_project, test_prefix):
    session_id = f"{test_prefix}-tool-lookup"
    await _queue_call(client, session_id, test_project, prompt="lookup prompt", ok=True)

    resp = await client.get(
        "/api/tool-calls",
        params={"project": test_project, "limit": 10},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    assert rows[0]["project_name"]
    assert "tool_name" in rows[0]


@pytest.mark.asyncio
async def test_export_dataset_sft_filters_problematic(client, test_project, test_prefix):
    session_id = f"{test_prefix}-sft-export"
    prompt = "run healthy command"
    await _queue_call(client, session_id, test_project, prompt=prompt, ok=True)
    await _queue_call(client, session_id, test_project, prompt=prompt, ok=False)

    # include_errors=false should remove problematic rows
    resp = await client.get(
        "/api/tool-calls/export/dataset",
        params={
            "dataset_type": "sft",
            "project": test_project,
            "include_errors": "false",
            "limit": 200,
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dataset_type"] == "sft"
    assert payload["count"] >= 1
    assert all(not item["meta"]["is_problematic"] for item in payload["items"])
    assert any(item["input"]["prompt_text"] == prompt for item in payload["items"])


@pytest.mark.asyncio
async def test_export_dataset_preference_pairs(client, test_project, test_prefix):
    session_id = f"{test_prefix}-pref-export"
    prompt = "same prompt to create preference pair"
    await _queue_call(client, session_id, test_project, prompt=prompt, ok=True)
    await _queue_call(client, session_id, test_project, prompt=prompt, ok=False)

    resp = await client.get(
        "/api/tool-calls/export/dataset",
        params={
            "dataset_type": "preference",
            "project": test_project,
            "include_errors": "true",
            "limit": 500,
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dataset_type"] == "preference"
    assert payload["count"] >= 1

    pair = next((item for item in payload["items"] if item["prompt_text"] == prompt), None)
    assert pair is not None
    assert pair["chosen"]["reward"] >= pair["rejected"]["reward"]
