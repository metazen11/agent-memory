"""Integration tests for /api/observations endpoints."""

import pytest

# Module-level storage for IDs created during test run
_created_obs_ids = []


@pytest.mark.asyncio
async def test_create_observation(client, test_project):
    resp = await client.post("/api/observations", json={
        "session_id": "obs-test-session",
        "project": test_project,
        "title": "Test observation creation",
        "subtitle": "Integration test",
        "type": "discovery",
        "narrative": "Verified that observation creation works end-to-end.",
        "facts": ["fact1", "fact2"],
        "concepts": ["testing"],
        "files_read": ["/tmp/test.py"],
        "files_modified": [],
        "tool_name": "Bash",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Test observation creation"
    assert data["type"] == "discovery"
    assert data["has_embedding"] is True
    assert data["id"] > 0
    _created_obs_ids.append(data["id"])


@pytest.mark.asyncio
async def test_create_observation_normalizes_type(client, test_project):
    """Type 'fix' should be normalized to 'bugfix' before DB insert."""
    resp = await client.post("/api/observations", json={
        "session_id": "obs-test-session",
        "project": test_project,
        "title": "Test type normalization",
        "type": "fix",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "bugfix", f"Expected 'bugfix', got '{data['type']}'"
    _created_obs_ids.append(data["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_type,expected", [
    ("bug", "bugfix"),
    ("update", "change"),
    ("cleanup", "refactor"),
    ("new", "feature"),
    ("xyz", "discovery"),
])
async def test_create_observation_type_aliases(client, test_project, raw_type, expected):
    resp = await client.post("/api/observations", json={
        "session_id": "obs-test-session",
        "project": test_project,
        "title": f"Type alias test: {raw_type}",
        "type": raw_type,
    })
    assert resp.status_code == 200
    assert resp.json()["type"] == expected


@pytest.mark.asyncio
async def test_get_observation_by_id(client):
    if not _created_obs_ids:
        pytest.skip("No observations created yet")
    obs_id = _created_obs_ids[0]
    resp = await client.get(f"/api/observations/{obs_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == obs_id
    assert data["title"] == "Test observation creation"


@pytest.mark.asyncio
async def test_get_nonexistent_observation_returns_404(client):
    resp = await client.get("/api/observations/999999999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_observations(client, test_project):
    resp = await client.get("/api/observations", params={
        "project": test_project,
        "limit": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_list_observations_with_type_filter(client, test_project):
    resp = await client.get("/api/observations", params={
        "project": test_project,
        "type": "bugfix",
        "limit": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert all(obs["type"] == "bugfix" for obs in data)


@pytest.mark.asyncio
async def test_search_observations_hybrid(client, test_project):
    resp = await client.post("/api/observations/search", json={
        "query": "test observation creation",
        "project": test_project,
        "limit": 5,
        "mode": "hybrid",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "observations" in data
    assert data["mode"] == "hybrid"
    assert data["total"] >= 0


@pytest.mark.asyncio
async def test_search_observations_vector_only(client, test_project):
    resp = await client.post("/api/observations/search", json={
        "query": "test observation",
        "project": test_project,
        "limit": 3,
        "mode": "vector",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "vector"


@pytest.mark.asyncio
async def test_search_observations_fts_only(client, test_project):
    resp = await client.post("/api/observations/search", json={
        "query": "observation creation",
        "project": test_project,
        "limit": 3,
        "mode": "fts",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "fts"
