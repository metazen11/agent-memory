"""Shared fixtures for agent-memory tests.

Tests run against the LIVE FastAPI app + Postgres (integration tests).
This is intentional — we want to verify real DB behavior, not mocks.

The test session uses a unique prefix to avoid polluting real data.
"""

import os
import sys
import uuid
from pathlib import Path

# Make the repo root importable so test modules can `from app... import ...`.
# pytest.ini's `pythonpath = .` does this for newer pytest configs; this
# fallback covers both setups.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest
import httpx

# Point at the running server
BASE_URL = os.environ.get("AGENT_MEMORY_TEST_URL", "http://localhost:3377")

# Unique prefix for this test run to isolate test data
TEST_PREFIX = f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def client():
    """httpx async client pointed at the live server. Per-test scope.

    Sends ``X-Agent-Name: claude`` so the trusted-agent bypass treats the
    test as a known localhost caller (the same path Claude hooks use).
    """
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=10.0,
        headers={"X-Agent-Name": "claude"},
    ) as c:
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
