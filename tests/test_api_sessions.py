"""Integration tests for /api/sessions endpoints."""

import pytest


@pytest.mark.asyncio
async def test_create_session(client, test_session_id, test_project):
    resp = await client.post("/api/sessions", json={
        "session_id": test_session_id,
        "project": test_project,
        "project_path": test_project,
        "agent_type": "claude-code",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == test_session_id
    assert data["agent_type"] == "claude-code"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_create_duplicate_session_returns_409(client, test_session_id, test_project):
    resp = await client.post("/api/sessions", json={
        "session_id": test_session_id,
        "project": test_project,
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_sessions(client, test_project):
    resp = await client.get("/api/sessions", params={"project": test_project})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_update_session(client, test_session_id):
    resp = await client.patch(f"/api/sessions/{test_session_id}", json={
        "status": "completed",
        "summary": "Test session completed",
    })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_nonexistent_session_returns_404(client, test_prefix):
    resp = await client.patch(f"/api/sessions/{test_prefix}-nonexistent", json={
        "status": "completed",
    })
    assert resp.status_code == 404
