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

    Sends ``X-Agent-Name: pytest`` — a trusted agent, so the trusted-agent
    bypass still treats the suite as a known localhost caller, but a *distinct*
    one from ``claude``.

    That distinction is load bearing (issue #63). The rate limiter keys its
    token bucket on this identity, and live Claude Code hooks send
    ``X-Agent-Name: claude``. When the suite also claimed "claude", a hook
    firing during a test run drew from the same 100-writes/min budget, and the
    suite failed with ~40 spurious 429s.
    """
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=10.0,
        headers={"X-Agent-Name": "pytest"},
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


@pytest.fixture(scope="session", autouse=True)
async def _deactivate_leaked_test_lessons(test_project):
    """Deactivate lessons this run created, however the run ends.

    Why a fixture and not a test: cleanup used to live in
    ``test_api_lessons.py::test_deactivate_lesson``. A test only runs if
    collection reaches it, so an earlier failure, a ``-k`` filter, a
    ``-x`` abort, or a KeyboardInterrupt left rows behind — 171 inactive
    plus 2 *active* fixture lessons had accumulated by 2026-10-02, and the
    active ones were being served into live sessions by the
    UserPromptSubmit hook. Teardown here runs in all of those cases.

    Deactivate rather than DELETE: the rows are audit history, and
    ``active=false`` is what actually removes them from
    ``/api/lessons?active=true`` — the only path that reaches a session.

    Both scopes must be swept. ``GET /api/lessons`` with no ``project``
    returns ONLY ``project_id IS NULL`` rows (deliberate, see
    app/routes/lessons.py), so a project-scoped fixture is invisible there.
    The first version of this fixture checked only the global scope and
    duly leaked ``#261 'Test lesson'`` under project 67850.
    """
    yield

    titles = {"Test lesson", "Global test lesson", "Bad regex", "Broad-match attempt"}

    def is_fixture(title: str) -> bool:
        return (
            title in titles
            or TEST_PREFIX in title
            or "fixture" in title.lower()
        )

    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=10.0, headers={"X-Agent-Name": "pytest"}
    ) as c:
        seen: set[int] = set()
        # None  -> global rows; test_project -> this run's project-scoped rows.
        for scope in (None, test_project):
            params = {"active": "true", "limit": 100}
            if scope is not None:
                params["project"] = scope
            try:
                resp = await c.get("/api/lessons", params=params)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                rows = payload if isinstance(payload, list) else payload.get("lessons", [])
                for lesson in rows:
                    if not isinstance(lesson, dict):
                        continue
                    lid = lesson.get("id")
                    if lid in seen or not is_fixture(lesson.get("title") or ""):
                        continue
                    seen.add(lid)
                    await c.patch(f"/api/lessons/{lid}", json={"active": False})
            except (httpx.HTTPError, ValueError, KeyError):
                # Never fail a green suite on best-effort cleanup.
                continue
