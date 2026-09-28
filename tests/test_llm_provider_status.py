"""Tests for provider failure classification and the Anthropic circuit breaker.

These cover the three failures from issue #64:

  1. Credit exhaustion was indistinguishable from "no key configured".
  2. Every distill candidate re-paid the doomed call AND its 13s
     rate-limiter wait (~130s wasted per 10-candidate run).
  3. Nothing recorded which model actually wrote a lesson.

No real API calls are made anywhere in this file — the Anthropic client is
always a stub.
"""

from __future__ import annotations

import time

import pytest

import anthropic
import httpx

from app import llm_provider_status as ps
from app.llm_provider_status import ProviderStatus, classify_provider_error


@pytest.fixture(autouse=True)
def _clean_breaker():
    """Breaker state is process-global; isolate every test from its neighbours."""
    ps.reset()
    yield
    ps.reset()


# ── Helpers to build real SDK exceptions ──────────────────────

def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status: int, message: str):
    """Construct a real typed SDK error, as the client would raise it."""
    response = httpx.Response(status, request=_request())
    return cls(message, response=response, body=None)


def _billing_error() -> anthropic.BadRequestError:
    """The exact shape the exhausted key returns.

    Anthropic reports credit exhaustion as a 400 invalid_request_error —
    the same typed class and status as a genuinely malformed request.
    """
    return _status_error(
        anthropic.BadRequestError,
        400,
        "Error code: 400 - invalid_request_error: Your credit balance is "
        "too low to access the Anthropic API.",
    )


# ── (a) Error classification ──────────────────────────────────

def test_billing_error_classified_as_billing():
    """A 400 whose body says 'credit balance is too low' is a billing error.

    Status code alone cannot decide this: Anthropic uses 400 for both a
    malformed request and an unpayable account.
    """
    assert classify_provider_error(_billing_error()) == ProviderStatus.BILLING


def test_auth_errors_classified_as_auth():
    assert classify_provider_error(
        _status_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    ) == ProviderStatus.AUTH
    assert classify_provider_error(
        _status_error(anthropic.PermissionDeniedError, 403, "not permitted")
    ) == ProviderStatus.AUTH


def test_transient_errors_classified_as_transient():
    """429 / 5xx / connection failures must stay retryable."""
    assert classify_provider_error(
        _status_error(anthropic.RateLimitError, 429, "rate limited")
    ) == ProviderStatus.TRANSIENT
    assert classify_provider_error(
        _status_error(anthropic.InternalServerError, 529, "overloaded")
    ) == ProviderStatus.TRANSIENT
    assert classify_provider_error(
        anthropic.APIConnectionError(request=_request())
    ) == ProviderStatus.TRANSIENT
    assert classify_provider_error(
        anthropic.APITimeoutError(request=_request())
    ) == ProviderStatus.TRANSIENT


def test_ordinary_bad_request_is_not_billing():
    """A real malformed-request 400 must NOT be swallowed as billing.

    Misclassifying it would trip the breaker and hide a bug in our own
    request payload behind a "provider is broken" message.
    """
    exc = _status_error(
        anthropic.BadRequestError, 400, "messages.0.content: field required"
    )
    status = classify_provider_error(exc)
    assert status != ProviderStatus.BILLING
    assert status not in ps.NON_TRANSIENT


def test_billing_detected_without_status_code():
    """A stringified billing error with no status still classifies."""
    exc = Exception(
        "Error code: 400 - invalid_request_error: Your credit balance is too low"
    )
    assert classify_provider_error(exc) == ProviderStatus.BILLING


# ── (b) Circuit breaker ───────────────────────────────────────

def test_breaker_opens_on_billing_and_blocks_further_attempts():
    assert ps.anthropic_available("sk-test") is True
    ps.record_failure(_billing_error())
    assert ps.anthropic_available("sk-test") is False


def test_breaker_opens_on_auth():
    ps.record_failure(_status_error(anthropic.AuthenticationError, 401, "bad key"))
    assert ps.anthropic_available("sk-test") is False


def test_breaker_stays_closed_on_transient_errors():
    """A timeout or a 529 must NOT mask itself as a permanent outage."""
    for exc in (
        _status_error(anthropic.RateLimitError, 429, "slow down"),
        _status_error(anthropic.InternalServerError, 529, "overloaded"),
        anthropic.APITimeoutError(request=_request()),
    ):
        ps.record_failure(exc)
        assert ps.anthropic_available("sk-test") is True, exc


def test_breaker_reports_unavailable_when_no_key():
    assert ps.anthropic_available("") is False
    assert ps.anthropic_available(None) is False


def test_success_closes_a_transiently_failing_breaker():
    ps.record_failure(_status_error(anthropic.RateLimitError, 429, "slow down"))
    ps.record_success()
    snap = ps.snapshot("sk-test")
    assert snap["status"] == ProviderStatus.OK
    assert snap["circuit_open"] is False


def test_snapshot_distinguishes_no_key_from_out_of_credit():
    """The core issue-#64 confusion: these two must not look the same."""
    unconfigured = ps.snapshot("")
    assert unconfigured["configured"] is False
    assert unconfigured["status"] == ProviderStatus.NOT_CONFIGURED
    assert unconfigured["circuit_open"] is False

    ps.record_failure(_billing_error())
    broke = ps.snapshot("sk-test")
    assert broke["configured"] is True
    assert broke["status"] == ProviderStatus.BILLING
    assert broke["circuit_open"] is True


def test_snapshot_never_leaks_the_api_key():
    ps.record_failure(_billing_error())
    snap = ps.snapshot("sk-ant-secret-value")
    assert "sk-ant-secret-value" not in repr(snap)


# ── (b) No wasted rate-limiter waits ──────────────────────────

class _BillingClient:
    """Stub Anthropic client that always fails with the billing error."""

    def __init__(self):
        self.calls = 0
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        raise _billing_error()


@pytest.mark.asyncio
async def test_distill_stops_calling_anthropic_after_billing_error(monkeypatch):
    """After one billing failure, no further Anthropic attempts in the run.

    This is the ~130s-per-run waste from issue #64: synthesize_lesson()
    called Anthropic once per candidate and paid the full 13s
    _ANTHROPIC_MIN_INTERVAL wait before each doomed call.
    """
    import app.lesson_distill as ld
    import app.observation_llm as ol

    from app.config import settings

    client = _BillingClient()
    monkeypatch.setattr(ol, "_get_anthropic_client", lambda: client)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    # No local model configured, so synthesis returns None after Anthropic fails.
    monkeypatch.setattr(settings, "observation_llm_model", "", raising=False)
    # Pretend a call just happened so a naive implementation WOULD sleep 13s.
    monkeypatch.setattr(ol, "_last_anthropic_call", time.monotonic())

    candidate = {
        "project_name": "p",
        "tool_name": "Bash",
        "occurrences": 20,
        "sessions": 1,
        "sample_errors": ["fatal: not a git repository"],
        "sample_inputs": [],
        "normalized_error": "fatal: not a git repository",
    }

    # First candidate: pays the throttle (primed above), makes the call,
    # gets the billing error, trips the breaker.
    assert await ld.synthesize_lesson(candidate) is None
    assert client.calls == 1

    # Re-prime the throttle so a NON-breaking implementation would sleep
    # another 13s on each of the next four candidates — which is exactly
    # the ~130s-per-run waste issue #64 reported.
    monkeypatch.setattr(ol, "_last_anthropic_call", time.monotonic())

    started = time.monotonic()
    for _ in range(4):
        assert await ld.synthesize_lesson(candidate) is None
    elapsed = time.monotonic() - started

    # No further attempts, and — the real win — no further throttle waits.
    assert client.calls == 1, "breaker did not stop subsequent Anthropic attempts"
    assert elapsed < 1.0, (
        f"circuit breaker did not skip the throttle ({elapsed:.1f}s for 4 candidates)"
    )


@pytest.mark.asyncio
async def test_transient_error_still_retries_next_candidate(monkeypatch):
    """A 529 must not trip the breaker — the next candidate tries again."""
    import app.lesson_distill as ld
    import app.observation_llm as ol

    class _FlakyClient:
        def __init__(self):
            self.calls = 0
            self.messages = self

        async def create(self, **kwargs):
            self.calls += 1
            raise _status_error(anthropic.InternalServerError, 529, "overloaded")

    from app.config import settings

    client = _FlakyClient()
    monkeypatch.setattr(ol, "_get_anthropic_client", lambda: client)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    monkeypatch.setattr(settings, "observation_llm_model", "", raising=False)
    # Far enough in the past that the throttle does not sleep.
    monkeypatch.setattr(ol, "_last_anthropic_call", time.monotonic() - 100)

    candidate = {
        "project_name": "p",
        "tool_name": "Bash",
        "occurrences": 20,
        "sessions": 1,
        "sample_errors": ["boom"],
        "sample_inputs": [],
        "normalized_error": "boom",
    }

    await ld.synthesize_lesson(candidate)
    monkeypatch.setattr(ol, "_last_anthropic_call", time.monotonic() - 100)
    await ld.synthesize_lesson(candidate)

    assert client.calls == 2, "transient failure must not open the breaker"


@pytest.mark.asyncio
async def test_observation_capture_skips_anthropic_when_breaker_open(monkeypatch):
    """The breaker is shared: capture benefits from distillation's discovery."""
    import app.observation_llm as ol

    client = _BillingClient()
    monkeypatch.setattr(ol, "_get_anthropic_client", lambda: client)
    monkeypatch.setattr(ol.settings, "anthropic_api_key", "sk-test", raising=False)
    monkeypatch.setattr(ol.settings, "observation_llm_model", "", raising=False)
    monkeypatch.setattr(ol, "_last_anthropic_call", time.monotonic() - 100)

    args = ("Bash", {"command": "ls"}, "out", "/tmp", "do a thing")
    assert await ol.generate_observation(*args) is None
    assert client.calls == 1

    # Breaker is now open — a second capture must not call the API at all.
    assert await ol.generate_observation(*args) is None
    assert client.calls == 1


# ── (d) Provider recorded on the synthesized lesson ───────────

@pytest.mark.asyncio
async def test_synthesized_lesson_records_provider(monkeypatch):
    """A lesson written by Anthropic is tagged with the Anthropic provider."""
    import app.lesson_distill as ld
    import app.observation_llm as ol

    class _Text:
        text = (
            '{"skip": false, "title": "Check the repo root first", '
            '"rule": "Run `git -C public_html rev-parse --git-dir` before any git '
            'command targeting public_html; it is a plain directory here.", '
            '"severity": "warning"}'
        )

    class _Msg:
        content = [_Text()]

    class _OkClient:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            return _Msg()

    from app.config import settings

    monkeypatch.setattr(ol, "_get_anthropic_client", lambda: _OkClient())
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test", raising=False)
    monkeypatch.setattr(ol, "_last_anthropic_call", time.monotonic() - 100)

    result = await ld.synthesize_lesson({
        "project_name": "p",
        "tool_name": "Bash",
        "occurrences": 20,
        "sessions": 1,
        "sample_errors": ["fatal: not a git repository"],
        "sample_inputs": [],
        "normalized_error": "fatal: not a git repository",
    })

    assert result is not None
    assert result["_provider"] == ld.PROVIDER_ANTHROPIC
