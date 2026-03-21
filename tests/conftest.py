"""Shared fixtures for agent-memory tests.

Tests run against the LIVE FastAPI app + Postgres (integration tests).
This is intentional — we want to verify real DB behavior, not mocks.

The test session uses a unique prefix to avoid polluting real data.
"""

import os
import uuid

import pytest
import httpx

# Point at the running server
BASE_URL = os.environ.get("AGENT_MEMORY_TEST_URL", "http://localhost:3377")

# Unique prefix for this test run to isolate test data
TEST_PREFIX = f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def client():
    """httpx async client pointed at the live server. Per-test scope."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as c:
        yield c


@pytest.fixture(scope="session")
def test_prefix():
    """Unique prefix for isolating test data."""
    return TEST_PREFIX


@pytest.fixture(scope="session")
def test_project(test_prefix):
    """A unique project path for test data."""
    return f"/tmp/{test_prefix}/my-project"


@pytest.fixture(scope="session")
def test_session_id(test_prefix):
    """A unique session ID for test data."""
    return f"{test_prefix}-session"
