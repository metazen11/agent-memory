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
async def test_health_has_llm_section(client):
    """/api/health reports LLM provider state.

    Added for issue #64: credit exhaustion on the Anthropic key silently
    degraded lesson quality to the local model with nothing surfaced to
    the operator.
    """
    resp = await client.get("/api/health")
    data = resp.json()
    assert "llm" in data
    assert "local_model_configured" in data["llm"]

    providers = data["llm"]["providers"]
    assert isinstance(providers, list) and providers

    anthropic_status = next(p for p in providers if p["provider"] == "anthropic")
    assert "configured" in anthropic_status
    assert "circuit_open" in anthropic_status
    # "not configured" must be its own state, distinct from a billing or
    # auth failure — conflating them is the bug this section fixes.
    assert anthropic_status["status"] in (
        "ok", "not_configured", "auth_error", "billing_error",
        "transient_error", "unknown_error",
    )
    if not anthropic_status["configured"]:
        assert anthropic_status["status"] == "not_configured"


@pytest.mark.asyncio
async def test_health_llm_check_is_not_a_live_call(client):
    """The LLM section must report stored state, never probe the provider.

    A live probe would bill the account on every health poll and would
    block on the 13s rate limiter. Two back-to-back calls returning
    promptly with identical provider state is the observable signature of
    a cached read.
    """
    import time

    started = time.monotonic()
    first = await client.get("/api/health")
    second = await client.get("/api/health")
    elapsed = time.monotonic() - started

    # A single live Anthropic call would cost at least the 13s throttle.
    assert elapsed < 10.0, f"health check appears to make a live LLM call ({elapsed:.1f}s)"

    a = next(p for p in first.json()["llm"]["providers"] if p["provider"] == "anthropic")
    b = next(p for p in second.json()["llm"]["providers"] if p["provider"] == "anthropic")
    assert a["status"] == b["status"]
    assert a["circuit_open"] == b["circuit_open"]


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
