"""Validate the subprocess boundary, cancellation and observation retry contract."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app import anvil_enrichment as anvil
from app import observation_llm


class Process:
    pid = 123456789
    returncode = None

    def __init__(self, content, delay=0):
        self.content = content
        self.delay = delay
        self.request = None

    @property
    def stdin(self):
        return self

    @property
    def stdout(self):
        if not hasattr(self, "reader"):
            self.reader = asyncio.StreamReader()
            self.reader.feed_data(
                json.dumps(
                    {"content": json.dumps(self.content), "provider": "anvil:mlx:test"}
                ).encode()
            )
            self.reader.feed_eof()
        return self.reader

    def write(self, request):
        self.request = request.decode()

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait(self):
        await asyncio.sleep(self.delay)
        self.returncode = 0


@pytest.mark.asyncio
async def test_valid_output_is_redacted_and_attributed(monkeypatch):
    process = Process(
        {"title": "Atomic replay", "narrative": "Replay returns the original receipt."}
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await anvil.generate_json(
        "system", "token sk-ant-api03-" + "x" * 100, "observation"
    )
    assert result["_provider"] == "anvil:mlx:test"
    assert "x" * 100 not in process.request
    assert spawn.call_args.kwargs["start_new_session"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"title": "x"}, ["wrong"], {"title": "Bad", "narrative": 3}]
)
async def test_malformed_output_remains_retryable(monkeypatch, payload):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=Process(payload))
    )
    with pytest.raises(anvil.EnrichmentUnavailable):
        await anvil.generate_json("system", "user", "observation")


@pytest.mark.asyncio
async def test_timeout_kills_process_group(monkeypatch):
    process = Process({}, delay=1)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(anvil.settings, "anvil_timeout_seconds", 0.01)
    killed = []
    monkeypatch.setattr(anvil.os, "killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(anvil.EnrichmentUnavailable):
        await anvil.generate_json("system", "user", "observation")
    assert killed == [process.pid]
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_unavailable_primary_uses_fallback_and_propagates_failures(monkeypatch):
    monkeypatch.setattr(observation_llm.settings, "anvil_fallback_enabled", True)
    monkeypatch.setattr(observation_llm.settings, "observation_llm_model", "")
    monkeypatch.setattr(
        observation_llm.provider_status, "anthropic_available", lambda _: False
    )
    monkeypatch.setattr(
        anvil,
        "generate_json",
        AsyncMock(side_effect=anvil.EnrichmentUnavailable("failed")),
    )
    with pytest.raises(anvil.EnrichmentUnavailable):
        await observation_llm.generate_observation(
            "Edit", {}, "change", "/tmp/project", "fix"
        )


@pytest.mark.asyncio
async def test_skip_is_an_intentional_noop(monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=Process({"skip": True})),
    )
    assert (await anvil.generate_json("system", "user", "observation"))["skip"] is True


@pytest.mark.asyncio
async def test_output_limit_terminates_without_unbounded_buffer(monkeypatch):
    process = Process({"title": "x" * 70000})
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    killed = []
    monkeypatch.setattr(anvil.os, "killpg", lambda pid, sig: killed.append(pid))
    with pytest.raises(anvil.EnrichmentUnavailable):
        await anvil.generate_json("system", "user", "observation")
    assert killed == [process.pid]


@pytest.mark.parametrize("tool_calls", [[], [{"name": "bash_run"}]])
def test_bridge_disables_tools_and_raw_logging(tmp_path, tool_calls):
    """Exercise the real bridge protocol without loading the host MLX model."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    package = tmp_path / "anvil"
    (package / "llm").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "llm/__init__.py").write_text("")
    (package / "config.py").write_text(
        'from types import SimpleNamespace\nsettings=SimpleNamespace(model_backend="test", model_name="model", mlx_thinking_enabled=True, llm_daemon_enabled=True)\n'
    )
    (package / "llm/raw_logging.py").write_text(
        'def log_raw_llm_interaction(**kwargs):\n    raise RuntimeError("raw logging enabled")\n'
    )
    (package / "runner.py").write_text('raise RuntimeError("agent runner imported")\n')
    (package / "llm/engine.py").write_text(
        "from anvil.llm import raw_logging\n"
        "from anvil.config import settings\n"
        "def chat_completion(**kwargs):\n"
        "    assert not settings.llm_daemon_enabled\n"
        '    assert kwargs["tools"] == []\n'
        '    assert kwargs["max_tokens"] == 900\n'
        '    raw_logging.log_raw_llm_interaction(secret="do not record")\n'
        '    print("model chatter")\n'
        f'    return {{"content": "{{}}", "tool_calls": {tool_calls!r}}}\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).absolute().parents[1] / "scripts/anvil_enrich.py"),
        ],
        input=json.dumps({"messages": []}),
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "AGENT_MEMORY_ANVIL_ROOT": str(tmp_path)},
        check=False,
    )
    if tool_calls:
        # Per-item failure: reported in-band (charged), not a crash (outage).
        assert result.returncode == 0
        assert "Enrichment requested tools" in json.loads(result.stdout)["error"]
    else:
        assert result.returncode == 0
        assert json.loads(result.stdout)["provider"] == "anvil:test:model"
        assert "model chatter" in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected", [("failed", "degraded"), ("timeout", "degraded"), ("ok", "ok")]
)
async def test_health_surfaces_fallback_failure(monkeypatch, status, expected):
    from contextlib import asynccontextmanager

    from app.routes import health as route

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            connection = AsyncMock()
            connection.fetchval.side_effect = ["PostgreSQL test", True, 0, 1]
            yield connection

    monkeypatch.setattr(route, "get_pool", AsyncMock(return_value=Pool()))
    monkeypatch.setattr(
        route, "check_embeddings", AsyncMock(return_value={"status": "ok"})
    )
    monkeypatch.setattr(
        route.llm_provider_status, "snapshot", lambda _: {"circuit_open": False}
    )
    monkeypatch.setattr(anvil.settings, "anvil_fallback_enabled", True)
    monkeypatch.setitem(anvil._status, "status", status)
    assert (await route.health())["status"] == expected


def test_observation_provenance_is_stored_without_changing_embedding_input():
    from app.queue_worker import _build_raw_text

    observation = {
        "title": "Atomic replay",
        "narrative": "Receipt replay is idempotent.",
        "_provider": "anvil:mlx:test",
    }
    assert "[enrichment_provider: anvil:mlx:test]" in _build_raw_text(observation)
    assert "anvil:" not in _build_raw_text(observation, include_provider=False)


@pytest.mark.asyncio
async def test_crashed_bridge_is_an_outage_with_logged_redacted_cause(
    monkeypatch, caplog
):
    """A dead bridge (e.g. missing model file) must say why, not fail silently."""
    import sys

    secret = "sk-ant-api03-" + "y" * 100
    script = (
        "import sys; sys.stdin.read(); "
        f"sys.stderr.write('ValueError: Model path does not exist {secret}'); "
        "sys.exit(1)"
    )
    monkeypatch.setattr(
        anvil, "_bridge_command", lambda **_: [sys.executable, "-c", script]
    )
    with pytest.raises(anvil.EnrichmentOutage):
        await anvil.generate_json("system", "user", "observation")
    assert "Model path does not exist" in caplog.text
    assert "y" * 100 not in caplog.text
    assert anvil.snapshot()["status"] == "failed"


@pytest.mark.asyncio
async def test_outage_releases_queue_item_without_charging_retry(monkeypatch):
    from contextlib import asynccontextmanager

    from app import queue_worker

    class Conn:
        def __init__(self):
            self.executed = []

        async def fetchrow(self, *_):
            return {
                "id": 7, "session_id": "s", "tool_name": "Edit", "tool_input": "{}",
                "tool_response_preview": "", "cwd": "/tmp", "last_user_message": "",
                "source_system": None, "source_mode": None, "source_agent": None,
            }

        async def execute(self, sql, *args):
            self.executed.append(sql)

    conn = Conn()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    monkeypatch.setattr(
        queue_worker,
        "generate_observation",
        AsyncMock(side_effect=anvil.EnrichmentOutage("down")),
    )
    with pytest.raises(anvil.EnrichmentOutage):
        await queue_worker.process_one(Pool())
    assert len(conn.executed) == 1
    assert "status = 'pending'" in conn.executed[0]
    assert "retry_count" not in conn.executed[0]


@pytest.mark.asyncio
async def test_per_item_bridge_error_is_charged_not_an_outage(monkeypatch):
    """One bad item must consume its retries, never stall the queue."""
    import sys

    script = (
        "import sys, json; sys.stdin.read(); "
        "print(json.dumps({'error': 'ValueError: context too long'}))"
    )
    monkeypatch.setattr(
        anvil, "_bridge_command", lambda **_: [sys.executable, "-c", script]
    )
    with pytest.raises(anvil.EnrichmentUnavailable) as raised:
        await anvil.generate_json("system", "user", "observation")
    assert not isinstance(raised.value, anvil.EnrichmentOutage)


@pytest.mark.asyncio
async def test_timeout_is_an_outage_that_still_charges(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=Process({}, delay=1))
    )
    monkeypatch.setattr(anvil.settings, "anvil_timeout_seconds", 0.01)
    monkeypatch.setattr(anvil.os, "killpg", lambda pid, sig: None)
    with pytest.raises(anvil.EnrichmentOutage) as raised:
        await anvil.generate_json("system", "user", "observation")
    assert raised.value.charge is True
    assert anvil.snapshot()["status"] == "timeout"
