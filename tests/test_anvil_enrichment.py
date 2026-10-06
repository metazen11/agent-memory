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
