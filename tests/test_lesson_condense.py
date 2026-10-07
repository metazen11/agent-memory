"""Unit tests for app.lesson_condense (issue #75).

Providers are stubbed: these pin the condenser's CONTRACT — post-processing,
validation, the single length-feedback retry, provider fallback order and
explicit rejection — not any model's output quality. The live Anvil call is
exercised by the manual smoke documented in the README.
"""

from __future__ import annotations

import pytest

from app import lesson_condense as lc

LONG = (
    "On 2026-08-17 the incident MV froze for three days because REFRESH "
    "CONCURRENTLY failed on a duplicate gid. " * 6
    + "Always run scripts/apply_schemas.py --check before applying migrations."
)


def _stub(monkeypatch, *, anvil=None, haiku=None):
    """Install scripted providers. Each script is a list of results; an
    Exception instance is raised instead of returned."""
    calls = {"anvil": [], "haiku": []}

    def make(name, script):
        async def provider(system: str, user: str):
            calls[name].append(user)
            if script is None or not script:
                raise lc.ProviderUnavailable(f"{name} not configured")
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item, f"{name}:test"
        return provider

    monkeypatch.setattr(lc, "_call_anvil", make("anvil", anvil))
    monkeypatch.setattr(lc, "_call_haiku", make("haiku", haiku))
    return calls


# ── Post-processing ───────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("```\nRun X before Y.\n```", "Run X before Y."),
        ("```text\nRun X before Y.\n```", "Run X before Y."),
        ("<think>long reasoning</think>\nRun X before Y.", "Run X before Y."),
        ("CRITICAL [fire-map]: Run X before Y.", "Run X before Y."),
        ("CRITICAL: Run X before Y.", "Run X before Y."),
        ("Rule: Run X before Y.", "Run X before Y."),
        ('"Run X before Y."', "Run X before Y."),
        ("  Run X\n  before Y.  ", "Run X before Y."),
    ],
)
def test_clean_rule_strips_wrappers(raw, expected):
    assert lc.clean_rule(raw) == expected


def test_clean_rule_keeps_commands_and_flags_intact():
    raw = "Before `git push`, run `scripts/ci_gate.py --strict` and check exit code."
    assert lc.clean_rule(raw) == raw


def test_clean_rule_unterminated_think_is_empty():
    # A model that never closed its think block produced no rule at all.
    assert lc.clean_rule("<think>still thinking about it") == ""


# ── prepare_rule: pass-through ────────────────────────────────

async def test_short_rule_passes_through_untouched(monkeypatch):
    calls = _stub(monkeypatch)
    out = await lc.prepare_rule("Run the gate first.", None)
    assert out == lc.PreparedRule(rule="Run the gate first.", detail=None, provider=None)
    assert calls == {"anvil": [], "haiku": []}


async def test_short_rule_keeps_caller_detail(monkeypatch):
    _stub(monkeypatch)
    out = await lc.prepare_rule("Run the gate first.", "Background story.")
    assert out.detail == "Background story."
    assert out.provider is None


async def test_exactly_280_chars_is_not_condensed(monkeypatch):
    calls = _stub(monkeypatch)
    rule = "x" * lc.MAX_RULE_CHARS
    out = await lc.prepare_rule(rule, None)
    assert out.rule == rule
    assert calls["anvil"] == []


# ── prepare_rule: condensing ──────────────────────────────────

async def test_long_rule_condensed_by_anvil_and_original_kept(monkeypatch):
    _stub(monkeypatch, anvil=["Run scripts/apply_schemas.py --check before migrations."])
    out = await lc.prepare_rule(LONG, None)
    assert out.rule == "Run scripts/apply_schemas.py --check before migrations."
    assert out.detail == LONG
    assert out.provider == "anvil:test"


async def test_caller_detail_is_appended_after_original(monkeypatch):
    _stub(monkeypatch, anvil=["Short rule."])
    out = await lc.prepare_rule(LONG, "Extra context.")
    assert out.detail == LONG + "\n\nExtra context."


async def test_too_long_output_retried_once_with_length_feedback(monkeypatch):
    calls = _stub(monkeypatch, anvil=["y" * 400, "Short enough rule."])
    out = await lc.prepare_rule(LONG, None)
    assert out.rule == "Short enough rule."
    assert len(calls["anvil"]) == 2
    assert "400 characters" in calls["anvil"][1]
    assert f"at most {lc.RETRY_TARGET_CHARS}" in calls["anvil"][1]
    assert "y" * 400 not in calls["anvil"][1], "retry must not quote the over-long answer"
    assert calls["haiku"] == []


async def test_anvil_still_too_long_falls_back_to_haiku(monkeypatch):
    calls = _stub(monkeypatch, anvil=["y" * 400, "z" * 300], haiku=["Haiku rule."])
    out = await lc.prepare_rule(LONG, None)
    assert out.rule == "Haiku rule."
    assert out.provider == "haiku:test"
    assert len(calls["anvil"]) == 2


async def test_anvil_unavailable_falls_back_to_haiku(monkeypatch):
    _stub(monkeypatch, anvil=[lc.ProviderUnavailable("down")], haiku=["Haiku rule."])
    out = await lc.prepare_rule(LONG, None)
    assert out.provider == "haiku:test"


async def test_empty_output_counts_as_failure(monkeypatch):
    calls = _stub(monkeypatch, anvil=["```\n```", "<think>x</think>"], haiku=["Ok rule."])
    out = await lc.prepare_rule(LONG, None)
    assert out.rule == "Ok rule."
    assert len(calls["anvil"]) == 2


async def test_all_providers_fail_rejects_without_truncating(monkeypatch):
    _stub(monkeypatch, anvil=["y" * 400, "y" * 400], haiku=[lc.ProviderUnavailable("no key")])
    with pytest.raises(lc.CondenseRejected) as info:
        await lc.prepare_rule(LONG, None)
    msg = str(info.value)
    assert "280" in msg
    assert "anvil" in msg and "haiku" in msg


async def test_no_providers_configured_rejects(monkeypatch):
    _stub(monkeypatch)
    with pytest.raises(lc.CondenseRejected):
        await lc.prepare_rule(LONG, None)


def test_condense_rejected_is_a_value_error():
    # Callers (API, MCP, distiller) catch ValueError-family errors; a
    # rejection must never be mistaken for an internal crash.
    assert issubclass(lc.CondenseRejected, ValueError)


# ── Anvil env pinning ─────────────────────────────────────────

def test_anvil_env_pins_backend_and_model(monkeypatch):
    monkeypatch.setattr(lc.settings, "anvil_condense_backend", "mlx")
    monkeypatch.setattr(lc.settings, "anvil_condense_model_path", "~/models/m")
    env = lc.anvil_model_env()
    assert env["ANVIL_MODEL_BACKEND"] == "mlx"
    assert env["ANVIL_MODEL_NAME"] == ""
    assert env["ANVIL_MODEL_PATH"].endswith("/models/m")
    assert not env["ANVIL_MODEL_PATH"].startswith("~")


# ── Anvil bridge: plain-text mode ─────────────────────────────

class _BridgeProcess:
    pid = 987654321
    returncode = None

    def __init__(self, envelope: bytes):
        self._envelope = envelope
        self.request = None

    @property
    def stdin(self):
        return self

    @property
    def stdout(self):
        import asyncio
        if not hasattr(self, "reader"):
            self.reader = asyncio.StreamReader()
            self.reader.feed_data(self._envelope)
            self.reader.feed_eof()
        return self.reader

    def write(self, data):
        self.request = data.decode()

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait(self):
        self.returncode = 0


async def test_generate_text_returns_raw_text_and_pins_env(monkeypatch):
    """The condenser asks for PLAIN TEXT: the model emitted invalid JSON
    (unescaped quotes in `--reason "..."`) when asked for a JSON object."""
    import asyncio
    import json
    from unittest.mock import AsyncMock

    from app import anvil_enrichment as anvil

    text = 'Run `tool --reason "why"` before deleting.'
    proc = _BridgeProcess(json.dumps({"content": text, "provider": "anvil:mlx:m"}).encode())
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    content, provider = await anvil.generate_text(
        "sys", "user", env_overrides={"ANVIL_MODEL_BACKEND": "mlx"}
    )
    assert content == text
    assert provider == "anvil:mlx:m"
    assert spawn.call_args.kwargs["env"]["ANVIL_MODEL_BACKEND"] == "mlx"


async def test_missing_pinned_model_path_fails_closed(monkeypatch, tmp_path):
    """With a nonexistent pinned path, Anvil silently auto-discovered ANOTHER
    MLX model. The condenser must refuse before spawning the bridge."""
    from unittest.mock import AsyncMock

    from app import anvil_enrichment

    spawn = AsyncMock()
    monkeypatch.setattr(anvil_enrichment, "generate_text", spawn)
    monkeypatch.setattr(lc.settings, "anvil_condense_enabled", True)
    monkeypatch.setattr(lc.settings, "anvil_condense_model_path", str(tmp_path / "nope"))
    with pytest.raises(lc.ProviderUnavailable, match="does not exist"):
        await lc._call_anvil("s", "u")
    spawn.assert_not_called()


async def test_existing_pinned_model_path_is_used(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from app import anvil_enrichment

    spawn = AsyncMock(return_value=("Rule.", "anvil:mlx:m"))
    monkeypatch.setattr(anvil_enrichment, "generate_text", spawn)
    monkeypatch.setattr(lc.settings, "anvil_condense_enabled", True)
    monkeypatch.setattr(lc.settings, "anvil_condense_model_path", str(tmp_path))
    assert await lc._call_anvil("s", "u") == ("Rule.", "anvil:mlx:m")
    assert spawn.call_args.kwargs["env_overrides"]["ANVIL_MODEL_PATH"] == str(tmp_path)
