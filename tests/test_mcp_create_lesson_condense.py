"""MCP create_lesson goes through the condenser (issue #75)."""

import json

import pytest

import mcp_server
from app import lesson_condense


class _Conn:
    def __init__(self):
        self.insert_args = None

    async def fetchrow(self, sql, *args):
        if "INSERT INTO mem_lessons" in sql:
            self.insert_args = (sql, args)
            return {"id": 4242}
        return {"id": 1}


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


@pytest.fixture(autouse=True)
def _no_embed(monkeypatch):
    async def none(_text):
        return None
    monkeypatch.setattr(mcp_server, "try_embed", none)


async def test_long_rule_is_condensed_and_original_stored_in_detail(monkeypatch):
    long_rule = "Backstory. " * 60 + "Do X."

    async def fake(rule, detail=None):
        assert rule == long_rule
        return lesson_condense.PreparedRule(rule="Do X first.", detail=rule, provider="anvil:mlx:m")
    monkeypatch.setattr(mcp_server, "prepare_rule", fake)

    pool = _Pool()
    out = await mcp_server._create_lesson(pool, {"rule": long_rule, "trigger_tool": "Bash"})
    payload = json.loads(out[0].text)
    sql, args = pool.conn.insert_args
    assert "detail" in sql
    assert "Do X first." in args
    assert long_rule in args
    assert payload["condensed_by"] == "anvil:mlx:m"


async def test_rejection_surfaces_as_error_and_nothing_is_inserted(monkeypatch):
    async def fake(rule, detail=None):
        raise lesson_condense.CondenseRejected("rule is 700 chars (max 280); all providers failed")
    monkeypatch.setattr(mcp_server, "prepare_rule", fake)

    pool = _Pool()
    with pytest.raises(lesson_condense.CondenseRejected):
        await mcp_server._create_lesson(pool, {"rule": "x" * 700, "trigger_tool": "Bash"})
    assert pool.conn.insert_args is None
