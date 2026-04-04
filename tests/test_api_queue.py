"""Integration tests for /api/queue endpoint."""

import pytest


@pytest.mark.asyncio
async def test_queue_accepts_valid_payload(client, test_project):
    resp = await client.post("/api/queue", json={
        "session_id": "queue-test-session",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "tool_response_preview": "hello",
        "cwd": test_project,
        "last_user_message": "run echo",
        "source_system": "anvil",
        "source_mode": "cli",
        "source_agent": "dev",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_queue_accepts_minimal_payload(client):
    resp = await client.post("/api/queue", json={
        "session_id": "queue-test-minimal",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_queue_requires_session_id(client):
    resp = await client.post("/api/queue", json={
        "tool_name": "Bash",
    })
    assert resp.status_code == 422  # validation error
