"""Integration tests for /api/lessons endpoints."""

import pytest

# Module-level storage for IDs created during test run
_created_lesson_ids = []


@pytest.mark.asyncio
async def test_create_lesson(client, test_project):
    resp = await client.post("/api/lessons", json={
        "title": "Test lesson",
        "rule": "Always run tests before declaring done",
        "severity": "warning",
        "project": test_project,
        "trigger_tool": "Bash",
        "trigger_pattern": "git push",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test lesson"
    assert data["rule"] == "Always run tests before declaring done"
    assert data["severity"] == "warning"
    assert data["active"] is True
    _created_lesson_ids.append(data["id"])


@pytest.mark.asyncio
async def test_create_global_lesson(client):
    resp = await client.post("/api/lessons", json={
        "title": "Global test lesson",
        "rule": "Never skip validation",
        "severity": "critical",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] is None
    assert data["severity"] == "critical"
    _created_lesson_ids.append(data["id"])


@pytest.mark.asyncio
async def test_create_lesson_invalid_regex_returns_400(client):
    resp = await client.post("/api/lessons", json={
        "title": "Bad regex",
        "rule": "Some rule",
        "trigger_pattern": "[invalid(",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_lessons(client, test_project):
    if not _created_lesson_ids:
        pytest.skip("No lessons created")
    resp = await client.get("/api/lessons", params={
        "project": test_project,
        "active": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_match_lessons(client, test_project):
    if not _created_lesson_ids:
        pytest.skip("No lessons created")
    resp = await client.get("/api/lessons/match", params={
        "tool_name": "Bash",
        "tool_input_preview": "git push origin main",
        "project": test_project,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    # Our test lesson should match (trigger_tool=Bash, pattern="git push")
    matched_ids = [l["id"] for l in data]
    assert _created_lesson_ids[0] in matched_ids


@pytest.mark.asyncio
async def test_match_lessons_no_match(client, test_project):
    resp = await client.get("/api/lessons/match", params={
        "tool_name": "Read",
        "tool_input_preview": "cat /etc/hosts",
        "project": test_project,
    })
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_update_lesson(client):
    if not _created_lesson_ids:
        pytest.skip("No lessons created")
    resp = await client.patch(f"/api/lessons/{_created_lesson_ids[0]}", json={
        "severity": "critical",
    })
    assert resp.status_code == 200
    assert resp.json()["severity"] == "critical"


@pytest.mark.asyncio
async def test_update_nonexistent_lesson_returns_404(client):
    resp = await client.patch("/api/lessons/999999999", json={
        "title": "nope",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trigger_lesson(client):
    if not _created_lesson_ids:
        pytest.skip("No lessons created")
    resp = await client.post(f"/api/lessons/{_created_lesson_ids[0]}/trigger")
    assert resp.status_code == 200
    assert resp.json()["triggered"] is True


@pytest.mark.asyncio
async def test_deactivate_lesson(client):
    """Deactivate test lessons so they don't interfere with real sessions."""
    for lid in _created_lesson_ids:
        resp = await client.patch(f"/api/lessons/{lid}", json={"active": False})
        assert resp.status_code == 200
        assert resp.json()["active"] is False
