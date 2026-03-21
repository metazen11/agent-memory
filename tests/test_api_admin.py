"""Integration tests for /api/admin endpoints."""

import pytest


@pytest.mark.asyncio
async def test_admin_stats(client):
    resp = await client.get("/api/admin/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "observations" in data
    assert data["observations"]["total"] >= 0
    assert "sessions" in data
    assert "projects" in data
    assert "queue" in data
    assert "by_type" in data
    assert "by_project" in data


@pytest.mark.asyncio
async def test_reembed_status_idle(client):
    resp = await client.get("/api/admin/re-embed/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
