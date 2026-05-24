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


# Synthetic secret strings used by the redaction tests below. These match
# SECRET_PATTERNS in app/redact.py — the OpenAI-style key is 51 chars to
# clear the 48+ minimum length, the bearer token is 30 chars to clear the
# 20+ minimum. Keep these inline so the test is self-documenting and
# doesn't drift if patterns change.
_FAKE_OPENAI_KEY = "sk-" + "A" * 48
_FAKE_BEARER = "Bearer " + "B" * 30


@pytest.mark.asyncio
async def test_export_jsonl_redacts_secrets(client, test_project, test_prefix):
    """Regression: /api/tool-calls/export must redact secrets in tool_input,
    tool_response_preview, and prompt_text before writing JSONL output.

    The queue endpoint normalizes paths but does NOT redact, so raw secrets
    posted through /api/queue land in mem_tool_calls verbatim. The export
    boundary is the last line of defense before secrets leave the system in
    a fine-tuning dataset.
    """
    session_id = f"{test_prefix}-export-redact-jsonl"
    await client.post(
        "/api/queue",
        json={
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {
                "command": "echo ok",
                "headers": {"Authorization": _FAKE_BEARER},
            },
            "tool_response_preview": f"response with {_FAKE_OPENAI_KEY} embedded",
            "cwd": test_project,
            "last_user_message": f"prompt mentioning {_FAKE_OPENAI_KEY}",
            "source_system": "tests",
            "source_mode": "integration",
            "source_agent": "pytest",
        },
    )

    resp = await client.get(
        "/api/tool-calls/export",
        params={"project": test_project, "format": "jsonl"},
    )
    assert resp.status_code == 200
    body = resp.text

    assert _FAKE_OPENAI_KEY not in body, "OpenAI-style key leaked into JSONL export"
    assert _FAKE_BEARER not in body, "Bearer token leaked into JSONL export"
    assert "[REDACTED:openai_key]" in body
    assert "[REDACTED:bearer_token]" in body


@pytest.mark.asyncio
async def test_export_dataset_redacts_secrets(client, test_project, test_prefix):
    """Regression: /api/tool-calls/export/dataset must redact secrets across
    all dataset_type variants (sft, trajectory, preference) since they all
    flow through _base_record in app/dataset_exports.py.
    """
    session_id = f"{test_prefix}-export-redact-dataset"
    await client.post(
        "/api/queue",
        json={
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"token": _FAKE_OPENAI_KEY},
            "tool_response_preview": f"leaked {_FAKE_BEARER} in response",
            "cwd": test_project,
            "last_user_message": "harmless prompt",
            "source_system": "tests",
            "source_mode": "integration",
            "source_agent": "pytest",
        },
    )

    resp = await client.get(
        "/api/tool-calls/export/dataset",
        params={
            "dataset_type": "sft",
            "project": test_project,
            "include_errors": "true",
            "limit": 200,
        },
    )
    assert resp.status_code == 200
    body = resp.text

    assert _FAKE_OPENAI_KEY not in body, "OpenAI-style key leaked into SFT dataset export"
    assert _FAKE_BEARER not in body, "Bearer token leaked into SFT dataset export"
