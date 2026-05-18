"""Cross-agent compatibility tests for the /api/queue write path.

Verifies that the post-migration-013 ``ensure_project`` behaviour
(canonical git-root collapse) doesn't break tool-call ingestion from
Claude, Anvil, or Codex hooks — all three send slightly different shapes.

These tests run against the LIVE FastAPI server on localhost:3377 AND a
direct read-only Postgres connection. Writes go through the queue endpoint;
verification reads use SQL directly because the ``/api/tool-calls`` reader
is not currently mounted in app/main.py (separate pre-existing bug; flagged
in the PR description, not fixed here).

What we verify
--------------
* Each agent's call gets ingested without error.
* Sub-folder cwds resolve to the canonical git root (one project_id per
  repo, not one per sub-folder).
* Calls from the SAME repo via DIFFERENT agents collapse to the same
  canonical project_id.
* The new ``git_branch`` and ``git_sha`` columns get populated by the live
  writer.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def cross_agent_repo() -> dict:
    """A real git repo on disk with a `src/lib` sub-folder.

    Created OUTSIDE pytest's tmp_path tree because the production
    ephemeral-path detector (correctly) flags paths under
    ``pytest-of-<user>/`` as test-only and refuses to treat them as
    canonical projects. This integration test needs the live server to
    treat the repo as a real project, so we put it in a stable
    ``/tmp/agent-memory-test-<uuid>/`` directory and clean it up after.
    """
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(
        prefix="agent-memory-test-", dir="/tmp"
    ))
    try:
        repo = base / "my-repo"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "main")
        _run(repo, "git", "config", "user.email", "t@t")
        _run(repo, "git", "config", "user.name", "t")
        _run(repo, "git", "remote", "add", "origin",
             "https://github.com/test/cross-agent.git")
        (repo / "README").write_text("hi")
        _run(repo, "git", "add", "README")
        _run(repo, "git", "commit", "-q", "-m", "init")
        sub = repo / "src" / "lib"
        sub.mkdir(parents=True)
        yield {"root": repo, "subdir": sub}
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def db_conn():
    """Direct asyncpg connection for verifying writes hit the table."""
    dsn = os.environ.get(
        "AGENT_MEMORY_DSN",
        "postgresql://localhost:5432/agent_memory",
    )
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _queue_call(
    client,
    *,
    session_id: str,
    cwd: str,
    source_system: str,
    source_agent: str,
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    last_user_message: str = "do the thing",
) -> int:
    resp = await client.post(
        "/api/queue",
        json={
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input or {"command": "echo hi"},
            "tool_response_preview": "hi",
            "cwd": cwd,
            "last_user_message": last_user_message,
            "source_system": source_system,
            "source_agent": source_agent,
            "source_mode": "test",
        },
    )
    return resp.status_code


async def _tool_calls_for_session(
    conn: asyncpg.Connection, session_id: str
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT tc.id, tc.project_id, tc.tool_name, tc.git_branch, tc.git_sha,
               tc.source_system, tc.source_agent, p.full_path, p.git_remote,
               p.canonical_root_path, p.source_kind
        FROM mem_tool_calls tc
        JOIN mem_projects p   ON p.id  = tc.project_id
        JOIN mem_sessions s   ON s.id  = tc.session_id
        WHERE s.session_id = $1
        ORDER BY tc.id
        """,
        session_id,
    )


# ── The three agent shapes — each writes successfully ────────────────────

@pytest.mark.asyncio
async def test_claude_hook_shape_writes_successfully(
    client, cross_agent_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-claude-{uuid.uuid4().hex[:6]}"
    status = await _queue_call(
        client,
        session_id=session_id,
        cwd=str(cross_agent_repo["subdir"]),
        source_system="claude-code",
        source_agent="claude",
        tool_name="Bash",
        tool_input={"command": "ls -la"},
    )
    assert status == 200, f"queue POST returned {status}"
    rows = await _tool_calls_for_session(db_conn, session_id)
    assert len(rows) == 1
    assert rows[0]["source_agent"] == "claude"
    assert rows[0]["source_kind"] == "git"


@pytest.mark.asyncio
async def test_anvil_hook_shape_writes_successfully(
    client, cross_agent_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-anvil-{uuid.uuid4().hex[:6]}"
    status = await _queue_call(
        client,
        session_id=session_id,
        cwd=str(cross_agent_repo["subdir"]),
        source_system="anvil",
        source_agent="anvil",
        tool_name="bash_run",
        tool_input={"command": "pwd"},
    )
    assert status == 200, f"queue POST returned {status}"
    rows = await _tool_calls_for_session(db_conn, session_id)
    assert len(rows) == 1
    assert rows[0]["source_agent"] == "anvil"
    assert rows[0]["tool_name"] == "bash_run"


@pytest.mark.asyncio
async def test_codex_hook_shape_writes_successfully(
    client, cross_agent_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-codex-{uuid.uuid4().hex[:6]}"
    status = await _queue_call(
        client,
        session_id=session_id,
        cwd=str(cross_agent_repo["root"]),
        source_system="codex",
        source_agent="codex",
        tool_name="shell",
        tool_input={"command": "git status"},
    )
    assert status == 200, f"queue POST returned {status}"
    rows = await _tool_calls_for_session(db_conn, session_id)
    assert len(rows) == 1
    assert rows[0]["source_agent"] == "codex"


# ── Cross-agent semantics ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subdir_and_root_collapse_to_same_project(
    client, cross_agent_repo, test_prefix, db_conn
):
    """Sub-folder + repo-root calls end up under the same project_id."""
    sess_root = f"{test_prefix}-collapse-root-{uuid.uuid4().hex[:6]}"
    sess_sub  = f"{test_prefix}-collapse-sub-{uuid.uuid4().hex[:6]}"

    await _queue_call(
        client, session_id=sess_root, cwd=str(cross_agent_repo["root"]),
        source_system="claude-code", source_agent="claude",
    )
    await _queue_call(
        client, session_id=sess_sub, cwd=str(cross_agent_repo["subdir"]),
        source_system="claude-code", source_agent="claude",
    )

    root_rows = await _tool_calls_for_session(db_conn, sess_root)
    sub_rows  = await _tool_calls_for_session(db_conn, sess_sub)
    assert root_rows and sub_rows

    project_ids = {r["project_id"] for r in root_rows + sub_rows}
    assert len(project_ids) == 1, (
        f"sub-folder and root should share project_id; got {project_ids}"
    )
    # Both rows resolve to the canonical git root path.
    canonical_paths = {r["full_path"] for r in root_rows + sub_rows}
    assert canonical_paths == {str(cross_agent_repo["root"].resolve())}


@pytest.mark.asyncio
async def test_three_agents_same_repo_same_project_id(
    client, cross_agent_repo, test_prefix, db_conn
):
    """Claude + Anvil + Codex hitting the same repo all see one project_id."""
    sessions = {
        "claude": f"{test_prefix}-3a-claude-{uuid.uuid4().hex[:6]}",
        "anvil":  f"{test_prefix}-3a-anvil-{uuid.uuid4().hex[:6]}",
        "codex":  f"{test_prefix}-3a-codex-{uuid.uuid4().hex[:6]}",
    }
    for agent, sid in sessions.items():
        await _queue_call(
            client, session_id=sid, cwd=str(cross_agent_repo["subdir"]),
            source_system=agent, source_agent=agent,
        )

    all_rows: list[asyncpg.Record] = []
    for sid in sessions.values():
        all_rows.extend(await _tool_calls_for_session(db_conn, sid))
    assert len(all_rows) == 3

    project_ids = {r["project_id"] for r in all_rows}
    assert len(project_ids) == 1, (
        f"three agents in same repo should share project_id; got {project_ids}"
    )
    # All point at the same canonical git_remote.
    remotes = {r["git_remote"] for r in all_rows}
    assert remotes == {"https://github.com/test/cross-agent.git"}


@pytest.mark.asyncio
async def test_git_branch_and_sha_populated_on_new_writes(
    client, cross_agent_repo, test_prefix, db_conn
):
    """The new git_branch + git_sha columns are filled in by the live writer."""
    session_id = f"{test_prefix}-gitcols-{uuid.uuid4().hex[:6]}"
    await _queue_call(
        client, session_id=session_id, cwd=str(cross_agent_repo["root"]),
        source_system="claude-code", source_agent="claude",
    )
    rows = await _tool_calls_for_session(db_conn, session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["git_branch"] == "main", f"got branch {row['git_branch']!r}"
    assert row["git_sha"] is not None
    assert len(row["git_sha"]) == 40, f"got sha {row['git_sha']!r}"


@pytest.mark.asyncio
async def test_no_new_row_per_subdir_after_consolidation(
    client, cross_agent_repo, test_prefix, db_conn
):
    """A second-level subdir call doesn't create yet another mem_projects row."""
    # Two distinct subdir cwds, both inside the same repo.
    deep_a = cross_agent_repo["root"] / "src" / "a"
    deep_b = cross_agent_repo["root"] / "src" / "b"
    deep_a.mkdir(parents=True, exist_ok=True)
    deep_b.mkdir(parents=True, exist_ok=True)

    before = await db_conn.fetchval(
        "SELECT count(*) FROM mem_projects WHERE full_path = $1",
        str(cross_agent_repo["root"].resolve()),
    )
    assert before == 1, "canonical row should already exist from earlier tests"

    sid_a = f"{test_prefix}-deep-a-{uuid.uuid4().hex[:6]}"
    sid_b = f"{test_prefix}-deep-b-{uuid.uuid4().hex[:6]}"
    await _queue_call(
        client, session_id=sid_a, cwd=str(deep_a),
        source_system="claude-code", source_agent="claude",
    )
    await _queue_call(
        client, session_id=sid_b, cwd=str(deep_b),
        source_system="claude-code", source_agent="claude",
    )

    # The number of canonical rows for this repo should still be exactly 1.
    after = await db_conn.fetchval(
        "SELECT count(*) FROM mem_projects WHERE full_path = $1",
        str(cross_agent_repo["root"].resolve()),
    )
    assert after == 1, (
        f"deep subdir calls should not create new canonical rows; got {after}"
    )

    # And no leaf rows for deep_a / deep_b should appear in mem_projects.
    leaf_rows = await db_conn.fetch(
        "SELECT full_path FROM mem_projects WHERE full_path = ANY($1::text[])",
        [str(deep_a), str(deep_b)],
    )
    assert leaf_rows == [], (
        f"subdir cwds should not get their own mem_projects rows; got {leaf_rows}"
    )
