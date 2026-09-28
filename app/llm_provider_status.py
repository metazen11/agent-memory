"""Provider failure classification and a per-process Anthropic circuit breaker.

Why this exists
---------------
Both callers of the Anthropic API — observation capture
(``app.observation_llm.generate_observation``) and lesson synthesis
(``app.lesson_distill.synthesize_lesson``) — fall back to a local GGUF
model when the API call fails. That fallback is correct and stays. What
was broken is everything around it:

1. The degradation was SILENT. When the configured key hit

       Error code: 400 - invalid_request_error:
       Your credit balance is too low to access the Anthropic API.

   the only trace was one ``logger.warning`` per call. From the outside,
   "out of credit" looked exactly like "no key configured" — and lesson
   quality silently dropped to whatever the local 7B produces (it emitted
   "Set replace_all=True in edit_file calls", which is both too thin and
   actively wrong advice). Nothing surfaced in ``/api/health``, so an
   operator had no way to notice.

2. Every candidate RE-PAID the doomed call. ``synthesize_lesson`` calls
   Anthropic once per candidate, and each call first waits out the 13s
   rate-limiter interval (``_ANTHROPIC_MIN_INTERVAL``). A 10-candidate
   distill run therefore burned ~130 seconds sleeping before calls that
   could not possibly succeed, every run.

The fix is to classify the failure and, for failures that cannot resolve
themselves within a run, stop trying. A billing or auth failure is a
property of the account, not of the request: retrying it with the same key
yields the identical error. A timeout or a 529 is the opposite — transient
by definition — so the breaker deliberately does NOT trip on those.

Scope of the breaker
--------------------
The breaker is per-process and lives here (not in either caller) so both
callers share one view of provider health: if capture discovers the key is
out of credit, distillation should not rediscover it 10 more times.

It is deliberately NOT persisted. Credit gets topped up and keys get
rotated out of band; a process restart is the natural, cheap way to retry.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Failure classification ────────────────────────────────────

# Statuses that are self-resolving: the same request may well succeed on a
# later attempt, so the breaker must never trip on these.
TRANSIENT_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class ProviderStatus:
    """Outcome classes for a provider call.

    String constants rather than an Enum so they serialize into the
    ``/api/health`` JSON body without a custom encoder.
    """

    OK = "ok"
    NOT_CONFIGURED = "not_configured"  # no API key set — not an error
    AUTH = "auth_error"               # key invalid, revoked, or lacks access
    BILLING = "billing_error"         # key valid but the account cannot pay
    TRANSIENT = "transient_error"     # timeout, 429, 5xx — retry is sane
    UNKNOWN = "unknown_error"         # unclassified; treated as transient


#: Statuses that will not resolve on retry within the same process.
#: Retrying these wastes the rate-limiter wait and produces the same error.
NON_TRANSIENT = frozenset({ProviderStatus.AUTH, ProviderStatus.BILLING})


def _is_billing_message(text: str) -> bool:
    """True when a 400 body is really "you cannot pay", not "bad request".

    THIS IS THE ONE PLACE WE MATCH ON MESSAGE TEXT, and it is isolated here
    deliberately. Anthropic reports credit exhaustion as a 400
    ``invalid_request_error`` — the exact same typed exception
    (``anthropic.BadRequestError``) and status code it uses for a genuinely
    malformed request. There is no ``error.type`` or header that separates
    them, so the status code alone cannot distinguish "your JSON is wrong"
    (a bug we must not hide) from "your account is out of credit" (a
    condition where retrying is pointless).

    The observed body is:

        Error code: 400 - invalid_request_error:
        Your credit balance is too low to access the Anthropic API.

    Matching is kept broad enough to survive minor rewording but narrow
    enough that an ordinary malformed-request 400 is NOT swallowed — a
    misclassified 400 would trip the breaker and mask a real bug in our own
    request payload.
    """
    low = text.lower()
    return (
        "credit balance is too low" in low
        or "insufficient credit" in low
        or ("billing" in low and "upgrade" in low)
        or "purchase credits" in low
    )


def classify_provider_error(exc: BaseException) -> str:
    """Map an exception from the Anthropic SDK onto a ProviderStatus.

    Prefers the SDK's typed exceptions and HTTP status codes over string
    matching; message text is consulted only for the 400-billing case that
    the status code genuinely cannot resolve (see ``_is_billing_message``).
    """
    # Connection-level failures never carry a status code. Import lazily so
    # this module stays importable when `anthropic` is not installed.
    try:
        import anthropic
    except ImportError:
        anthropic = None  # type: ignore[assignment]

    if anthropic is not None:
        # APIConnectionError covers timeouts and DNS/socket failures. Both
        # are transient by definition.
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderStatus.TRANSIENT
        # 401 invalid key, 403 key lacks access to the model/endpoint.
        # Neither improves by retrying with the same credential.
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return ProviderStatus.AUTH

    status = getattr(exc, "status_code", None)
    if status is None:
        # Some SDK wrappers surface the code on the nested response only.
        status = getattr(getattr(exc, "response", None), "status_code", None)

    if isinstance(status, int):
        if status in (401, 403):
            return ProviderStatus.AUTH
        if status == 400 and _is_billing_message(str(exc)):
            return ProviderStatus.BILLING
        if status in TRANSIENT_STATUSES:
            return ProviderStatus.TRANSIENT
        if 400 <= status < 500:
            # Other 4xx are our bug (bad payload, unknown model). Retrying
            # will not fix it, but it is NOT a provider-health problem, so
            # it stays unknown rather than tripping the breaker.
            return ProviderStatus.UNKNOWN
        if status >= 500:
            return ProviderStatus.TRANSIENT

    # No usable status code. Fall back to the billing text check — a
    # credit-exhaustion error re-raised as a plain Exception (as happens
    # when a wrapper stringifies it) must still be caught.
    if _is_billing_message(str(exc)):
        return ProviderStatus.BILLING

    return ProviderStatus.UNKNOWN


# ── Circuit breaker ───────────────────────────────────────────

@dataclass
class _BreakerState:
    """Last-known provider health for this process."""

    status: str = ProviderStatus.OK
    detail: str | None = None
    tripped_at: float | None = None
    # Monotonic clock for elapsed math, wall clock for operator-facing output.
    tripped_at_wall: float | None = None
    consecutive_failures: int = 0
    last_success_wall: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_state = _BreakerState()


def anthropic_available(api_key: str | None) -> bool:
    """True when it is worth attempting an Anthropic call right now.

    Returns False when no key is configured, or when the breaker has
    tripped on a non-transient failure. Callers must check this BEFORE
    paying the rate-limiter wait — skipping the wait is most of the win
    (a 10-candidate distill run wasted ~130s sleeping before doomed calls).
    """
    if not api_key:
        return False
    with _state.lock:
        return _state.status not in NON_TRANSIENT


def record_success() -> None:
    """Clear the breaker after a call that actually worked."""
    with _state.lock:
        _state.status = ProviderStatus.OK
        _state.detail = None
        _state.tripped_at = None
        _state.tripped_at_wall = None
        _state.consecutive_failures = 0
        _state.last_success_wall = time.time()


def record_failure(exc: BaseException) -> str:
    """Classify a failure, update last-known state, and maybe trip.

    Returns the ProviderStatus so the caller can log it. Transient
    failures update the counters but leave the breaker closed, so a
    timeout or a 529 still retries normally on the next call.
    """
    status = classify_provider_error(exc)
    with _state.lock:
        _state.consecutive_failures += 1
        _state.status = status
        # Truncate: provider messages can be long and this is echoed into
        # the health payload. Never include the key itself — the SDK does
        # not put it in the message, and we do not add it.
        _state.detail = str(exc)[:300]
        if status in NON_TRANSIENT:
            if _state.tripped_at is None:
                _state.tripped_at = time.monotonic()
                _state.tripped_at_wall = time.time()
                logger.error(
                    "Anthropic circuit breaker OPEN (%s): %s. "
                    "Falling back to the local model for the rest of this "
                    "process; lesson and observation quality will be lower. "
                    "Restart the process after fixing billing/credentials.",
                    status,
                    _state.detail,
                )
    return status


def reset() -> None:
    """Reset breaker state. For tests and for an explicit operator retry."""
    with _state.lock:
        _state.status = ProviderStatus.OK
        _state.detail = None
        _state.tripped_at = None
        _state.tripped_at_wall = None
        _state.consecutive_failures = 0
        _state.last_success_wall = None


def snapshot(api_key: str | None) -> dict:
    """Last-known provider health, for ``/api/health``.

    Reports STORED state only. This must never make a live API call: a
    health check that bills the account (or waits 13s on the rate limiter)
    is worse than no health check at all.
    """
    with _state.lock:
        status = _state.status
        detail = _state.detail
        tripped_wall = _state.tripped_at_wall
        failures = _state.consecutive_failures
        last_success = _state.last_success_wall

    if not api_key:
        # No key is a configuration choice, not a failure. Say so plainly
        # so "never configured" is distinguishable from "out of credit" —
        # the confusion that motivated this whole module.
        return {
            "provider": "anthropic",
            "configured": False,
            "status": ProviderStatus.NOT_CONFIGURED,
            "circuit_open": False,
            "detail": "ANTHROPIC_API_KEY is not set; local model only.",
        }

    return {
        "provider": "anthropic",
        "configured": True,
        "status": status,
        "circuit_open": status in NON_TRANSIENT,
        "consecutive_failures": failures,
        "detail": detail,
        "tripped_at": tripped_wall,
        "last_success": last_success,
    }
