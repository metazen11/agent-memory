"""WarmBridge: reuse one `anvil_enrich.py --serve` process per pinned model.

Uses a REAL subprocess running a tiny fake serve loop (same line protocol as
scripts/anvil_enrich.py --serve) so process reuse, idle shutdown and
crash recovery are exercised for real, not mocked.
"""

import asyncio
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


async def test_crash_is_unavailable_and_next_call_restarts(fake_bridge):
    env = {"ANVIL_MODEL_BACKEND": "mlx"}
    with pytest.raises(anvil.EnrichmentUnavailable):
        await anvil.generate_text("s", "crash", env_overrides=env, keep_warm_seconds=30)
    content, _ = await anvil.generate_text("s", "ok", env_overrides=env, keep_warm_seconds=30)
    assert content.startswith("ok|")


async def test_bridge_error_line_is_unavailable(fake_bridge):
    with pytest.raises(anvil.EnrichmentUnavailable):
        await anvil.generate_text("s", "error", env_overrides={"ANVIL_MODEL_BACKEND": "mlx"}, keep_warm_seconds=30)


async def test_timeout_kills_wedged_process(fake_bridge, monkeypatch):
    env = {"ANVIL_MODEL_BACKEND": "mlx"}
    first, _ = await anvil.generate_text("s", "warm", env_overrides=env, keep_warm_seconds=30)
    with pytest.raises(anvil.EnrichmentUnavailable):
        await anvil.generate_text("s", "hang", env_overrides=env, keep_warm_seconds=30, timeout=0.3)
    await asyncio.gather(*list(anvil._reapers))
    with pytest.raises(ProcessLookupError):
        os.kill(_pid(first), 0)
