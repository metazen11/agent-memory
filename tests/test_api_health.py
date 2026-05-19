"""Integration tests for /api/health endpoint."""

import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")


@pytest.mark.asyncio
async def test_health_has_db_section(client):
    resp = await client.get("/api/health")
    data = resp.json()
    assert "db" in data
    assert data["db"]["status"] == "ok"
    assert "version" in data["db"]
    assert data["db"]["pgvector"] is True


@pytest.mark.asyncio
async def test_health_has_embeddings_section(client):
    resp = await client.get("/api/health")
    data = resp.json()
    assert "embeddings" in data
    assert data["embeddings"]["status"] == "ok"
    assert data["embeddings"]["dimensions"] > 0


@pytest.mark.asyncio
async def test_health_has_queue_section(client):
    resp = await client.get("/api/health")
    data = resp.json()
    assert "queue" in data
    assert "pending" in data["queue"]
    assert "observations_total" in data["queue"]
    assert data["queue"]["observations_total"] >= 0


@pytest.mark.asyncio
async def test_integration_guide_returns_markdown(client):
    """GET /api/integration_guide returns the markdown doc verbatim."""
    resp = await client.get("/api/integration_guide")
    assert resp.status_code == 200
    body = resp.text
    # Stable section anchors — these would change if someone rewrote the
    # guide structure, and that's a signal worth catching.
    assert "# agent-memory — integration guide" in body
    assert "## Recipes — minimum viable wires" in body
    assert "Recipe 1 — lessons inject on every turn" in body
    assert "/api/lessons" in body
    assert "/api/recall" in body
    assert "/api/queue" in body
    # Sanity: the doc is non-trivially long.
    assert len(body) > 4000
