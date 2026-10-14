"""WarmBridge: reuse one `anvil_enrich.py --serve` process per pinned model.

Uses a REAL subprocess running a tiny fake serve loop (same line protocol as
scripts/anvil_enrich.py --serve) so process reuse, idle shutdown and
crash recovery are exercised for real, not mocked.
"""

import asyncio
import json
import os
import sys
import textwrap

import pytest

from app import anvil_enrichment as anvil

FAKE_SERVE = textwrap.dedent('''
    import json, os, sys
    for line in sys.stdin:
        req = json.loads(line)
        user = req["messages"][1]["content"]
        if user == "crash":
            print("engine died: sk-ant-api03-" + "y" * 100, file=sys.stderr)
            sys.exit(3)
        if user == "error":
            print(json.dumps({"error": "ValueError: boom"}), flush=True)
            continue
        if user == "hang":
            import time; time.sleep(30)
        print(json.dumps({
            "content": f"{user}|pid={os.getpid()}|backend={os.environ.get('ANVIL_MODEL_BACKEND')}",
            "provider": "anvil:" + os.environ.get("ANVIL_MODEL_BACKEND", "?") + ":fake",
        }), flush=True)
''')


@pytest.fixture
async def fake_bridge(tmp_path, monkeypatch):
    script = tmp_path / "fake_serve.py"
    script.write_text(FAKE_SERVE)
    monkeypatch.setattr(anvil, "_bridge_command", lambda serve=False: [sys.executable, str(script)])
    yield
    await anvil.close_warm_bridges()


def _pid(content: str) -> int:
    return int(content.split("pid=")[1].split("|")[0])


async def test_second_call_reuses_the_same_process(fake_bridge):
    env = {"ANVIL_MODEL_BACKEND": "mlx"}
    a, provider = await anvil.generate_text("s", "one", env_overrides=env, keep_warm_seconds=30)
    b, _ = await anvil.generate_text("s", "two", env_overrides=env, keep_warm_seconds=30)
    assert provider == "anvil:mlx:fake"
    assert "backend=mlx" in a
    assert _pid(a) == _pid(b)


async def test_different_pinned_model_gets_its_own_process(fake_bridge):
    a, _ = await anvil.generate_text("s", "x", env_overrides={"ANVIL_MODEL_BACKEND": "mlx"}, keep_warm_seconds=30)
    b, p = await anvil.generate_text("s", "x", env_overrides={"ANVIL_MODEL_BACKEND": "local"}, keep_warm_seconds=30)
    assert _pid(a) != _pid(b)
    assert p == "anvil:local:fake"


async def test_idle_timeout_kills_process(fake_bridge):
    env = {"ANVIL_MODEL_BACKEND": "mlx"}
    a, _ = await anvil.generate_text("s", "x", env_overrides=env, keep_warm_seconds=0.2)
    await asyncio.sleep(0.4)
    await asyncio.gather(*list(anvil._reapers))
    with pytest.raises(ProcessLookupError):
        os.kill(_pid(a), 0)
    b, _ = await anvil.generate_text("s", "x", env_overrides=env, keep_warm_seconds=0.2)
    assert _pid(b) != _pid(a)


async def test_crash_is_an_outage_and_next_call_restarts(fake_bridge, caplog):
    env = {"ANVIL_MODEL_BACKEND": "mlx"}
    with pytest.raises(anvil.EnrichmentOutage) as raised:
        await anvil.generate_text("s", "crash", env_overrides=env, keep_warm_seconds=30)
    assert not raised.value.charge
    assert "engine died" in caplog.text and "y" * 100 not in caplog.text
    content, _ = await anvil.generate_text("s", "ok", env_overrides=env, keep_warm_seconds=30)
    assert content.startswith("ok|")


async def test_bridge_error_line_is_charged_not_an_outage(fake_bridge):
    with pytest.raises(anvil.EnrichmentUnavailable) as raised:
        await anvil.generate_text("s", "error", env_overrides={"ANVIL_MODEL_BACKEND": "mlx"}, keep_warm_seconds=30)
    assert not isinstance(raised.value, anvil.EnrichmentOutage)


async def test_timeout_kills_wedged_process(fake_bridge, monkeypatch):
    env = {"ANVIL_MODEL_BACKEND": "mlx"}
    first, _ = await anvil.generate_text("s", "warm", env_overrides=env, keep_warm_seconds=30)
    with pytest.raises(anvil.EnrichmentOutage) as raised:
        await anvil.generate_text("s", "hang", env_overrides=env, keep_warm_seconds=30, timeout=0.3)
    assert raised.value.charge
    await asyncio.gather(*list(anvil._reapers))
    with pytest.raises(ProcessLookupError):
        os.kill(_pid(first), 0)


async def test_observation_enrichment_keeps_the_model_loaded(fake_bridge, monkeypatch):
    calls = []
    monkeypatch.setattr(anvil, "_parse_envelope", lambda line: calls.append(line) or ('{"skip": true}', "anvil:x:fake"))
    monkeypatch.setattr(anvil.settings, "anvil_keep_warm_seconds", 30)
    await anvil.generate_json("s", "a", "observation")
    await anvil.generate_json("s", "b", "observation")
    pids = {_pid(json.loads(line)["content"]) for line in calls}
    assert len(calls) == 2 and len(pids) == 1
