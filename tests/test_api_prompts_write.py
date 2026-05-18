"""Integration tests for POST /api/prompts (the live prompt-write path).

Runs against the live FastAPI server. Verifies that the new write path
closes the prompt-capture gap left by the missing UserPromptSubmit hook.

Cross-agent: every agent (Claude, Anvil, Codex) sends UserPromptSubmit
hook events with the same payload shape; the test exercises that shape
end-to-end.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg
import pytest


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def prompt_test_repo() -> dict:
    """A real git repo on disk, outside pytest's tmp tree (which is flagged
    as ephemeral by the production helper). Cleaned up after the module."""
    base = Path(tempfile.mkdtemp(prefix="agent-memory-test-", dir="/tmp"))
    try:
        repo = base / "prompt-test-repo"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "main")
        _run(repo, "git", "config", "user.email", "t@t")
        _run(repo, "git", "config", "user.name", "t")
        _run(repo, "git", "remote", "add", "origin",
             "https://github.com/test/prompts.git")
        (repo / "README").write_text("hi")
        _run(repo, "git", "add", "README")
        _run(repo, "git", "commit", "-q", "-m", "init")
        yield {"root": repo}
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def db_conn():
    dsn = os.environ.get(
        "AGENT_MEMORY_DSN",
        "postgresql://localhost:5432/agent_memory",
    )
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _post_prompt(client, *, session_id: str, prompt: str, cwd: str,
                       agent_name: str = "claude-code") -> dict:
    resp = await client.post(
        "/api/prompts",
        json={
            "session_id": session_id,
            "prompt": prompt,
            "cwd": cwd,
            "agent_name": agent_name,
        },
    )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    return resp.json()


async def _prompts_for_session(
    conn: asyncpg.Connection, session_id: str
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT up.id, up.prompt_number, up.prompt_text, up.agent_name,
               up.turn_index, up.content_hash, up.retention_class,
               up.project_id, p.full_path, p.git_remote
        FROM mem_user_prompts up
        LEFT JOIN mem_projects p ON p.id = up.project_id
        JOIN mem_sessions s ON s.id = up.session_id
        WHERE s.session_id = $1
        ORDER BY up.prompt_number
        """,
        session_id,
    )


# ── Basic write ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prompt_creates_row(
    client, prompt_test_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-p1-{uuid.uuid4().hex[:6]}"
    body = await _post_prompt(
        client,
        session_id=session_id,
        prompt="run the integration tests",
        cwd=str(prompt_test_repo["root"]),
    )
    assert body["status"] == "created"
    assert body["prompt_number"] == 1

    rows = await _prompts_for_session(db_conn, session_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["prompt_text"] == "run the integration tests"
    assert row["prompt_number"] == 1
    assert row["turn_index"] == 1
    assert row["retention_class"] == "live"
    assert row["content_hash"] is not None
    # Project resolved via the consolidated ensure_project.
    assert row["full_path"] == str(prompt_test_repo["root"].resolve())
    assert row["git_remote"] == "https://github.com/test/prompts.git"


# ── Idempotency ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_prompt_twice_is_idempotent(
    client, prompt_test_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-p2-{uuid.uuid4().hex[:6]}"
    first = await _post_prompt(
        client, session_id=session_id, prompt="same exact prompt",
        cwd=str(prompt_test_repo["root"]),
    )
    second = await _post_prompt(
        client, session_id=session_id, prompt="same exact prompt",
        cwd=str(prompt_test_repo["root"]),
    )
    assert first["status"] == "created"
    assert second["status"] == "exists"
    assert first["id"] == second["id"]
    assert first["prompt_number"] == second["prompt_number"]

    rows = await _prompts_for_session(db_conn, session_id)
    assert len(rows) == 1


# ── Multiple prompts increment prompt_number ───────────────────────────

@pytest.mark.asyncio
async def test_multiple_unique_prompts_get_sequential_numbers(
    client, prompt_test_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-p3-{uuid.uuid4().hex[:6]}"
    for i, text in enumerate(["first thing", "second thing", "third thing"], 1):
        body = await _post_prompt(
            client, session_id=session_id, prompt=text,
            cwd=str(prompt_test_repo["root"]),
        )
        assert body["status"] == "created"
        assert body["prompt_number"] == i

    rows = await _prompts_for_session(db_conn, session_id)
    assert [r["prompt_number"] for r in rows] == [1, 2, 3]
    assert [r["prompt_text"] for r in rows] == [
        "first thing", "second thing", "third thing"
    ]


# ── Cross-agent: same shape works for all three agents ─────────────────

@pytest.mark.asyncio
async def test_anvil_and_codex_can_post_prompts(
    client, prompt_test_repo, test_prefix, db_conn
):
    for agent in ("anvil", "codex"):
        session_id = f"{test_prefix}-{agent}-{uuid.uuid4().hex[:6]}"
        await _post_prompt(
            client, session_id=session_id,
            prompt=f"a prompt from {agent}",
            cwd=str(prompt_test_repo["root"]),
            agent_name=agent,
        )
        rows = await _prompts_for_session(db_conn, session_id)
        assert len(rows) == 1
        assert rows[0]["agent_name"] == agent


# ── Redaction ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_secrets_in_prompt_are_redacted_on_write(
    client, prompt_test_repo, test_prefix, db_conn
):
    session_id = f"{test_prefix}-secret-{uuid.uuid4().hex[:6]}"
    leaked = "use this key sk-ant-api03-" + "A" * 80 + " to do the thing"
    await _post_prompt(
        client, session_id=session_id, prompt=leaked,
        cwd=str(prompt_test_repo["root"]),
    )
    rows = await _prompts_for_session(db_conn, session_id)
    assert len(rows) == 1
    stored = rows[0]["prompt_text"]
    assert "sk-ant-api03-" not in stored
    assert "[REDACTED:" in stored


# ── No cwd falls back to 'unknown' project ─────────────────────────────

@pytest.mark.asyncio
async def test_missing_cwd_uses_unknown_project(
    client, test_prefix, db_conn
):
    session_id = f"{test_prefix}-nocwd-{uuid.uuid4().hex[:6]}"
    resp = await client.post(
        "/api/prompts",
        json={
            "session_id": session_id,
            "prompt": "a prompt with no cwd",
            "agent_name": "claude-code",
        },
    )
    assert resp.status_code == 200
    rows = await _prompts_for_session(db_conn, session_id)
    assert len(rows) == 1
    assert rows[0]["full_path"] == "unknown"
