"""Unit tests for rate-limit bucket keying — no DB or server required.

Regression cover for issue #63. ``RateLimitMiddleware`` used to key its token
bucket on ``request.state.token`` (only ever set when ``require_auth=true``,
which defaults to false) and otherwise on ``request.client.host``. With the
default config that collapsed every localhost caller into one shared
"127.0.0.1" bucket at 100 writes/min, so the pytest suite, manual CLI runs and
live Claude Code hooks all competed — producing ~40 spurious 429 failures.

These assert on ``rate_limit_client_id`` directly rather than over HTTP, so we
never need to fire 100+ real requests to prove a budget is separate.
"""

import pytest
from starlette.datastructures import Headers

from app.auth import MAX_AGENT_NAME_LEN, resolve_agent_identity, sanitize_agent_name
from app.config import settings
from app.middleware import rate_limit_client_id, rate_limit_scope


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeState:
    """Stands in for ``request.state``.

    Deliberately has no ``token`` attribute unless one is set, mirroring the
    real default-config case where ``AuthMiddleware`` never runs.
    """


class _FakeRequest:
    def __init__(self, headers: dict | None = None, host: str | None = "127.0.0.1", token=None):
        self.headers = Headers(headers or {})
        self.client = _FakeClient(host) if host else None
        self.state = _FakeState()
        if token is not None:
            self.state.token = token


# ── The core regression: distinct agents get distinct buckets ──────


class TestDistinctAgentsGetDistinctBuckets:
    def test_two_agent_names_do_not_share_a_bucket(self):
        """The whole point of #63: pytest and claude must not collide."""
        a = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "pytest"}))
        b = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "claude"}))
        assert a != b

    def test_same_agent_name_shares_a_bucket(self):
        """Two requests from the same agent must land in one bucket, or the
        limiter would not limit anything."""
        a = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "claude"}))
        b = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "claude"}))
        assert a == b

    def test_agent_name_beats_shared_ip(self):
        """Same IP, different agents -> still separate. This is the exact case
        the old ``request.client.host`` keying got wrong."""
        a = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "codex"}, host="127.0.0.1"))
        b = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "anvil"}, host="127.0.0.1"))
        assert a != b

    def test_user_agent_is_used_when_no_agent_header(self):
        keyed = rate_limit_client_id(_FakeRequest({"User-Agent": "codex/1.2"}))
        assert keyed == rate_limit_client_id(_FakeRequest({"X-Agent-Name": "codex"}))

    def test_agent_header_wins_over_user_agent(self):
        """X-Agent-Name is the explicit signal and must take precedence."""
        keyed = rate_limit_client_id(
            _FakeRequest({"X-Agent-Name": "anvil", "User-Agent": "codex/1.2"})
        )
        assert keyed == rate_limit_client_id(_FakeRequest({"X-Agent-Name": "anvil"}))

    def test_httpx_user_agent_does_not_collapse_distinct_agents(self):
        """Every httpx caller sends ``User-Agent: python-httpx/...`` and
        ``python-httpx`` is in the default trusted list. If identity resolution
        scanned both headers against that list in one pass, all of them — the
        pytest suite included — would collapse into one "python-httpx" bucket,
        recreating the very bug #63 fixes. X-Agent-Name must win outright."""
        ua = "python-httpx/0.27.0"
        a = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "pytest", "User-Agent": ua}))
        b = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "claude", "User-Agent": ua}))
        c = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "some-tool", "User-Agent": ua}))
        assert len({a, b, c}) == 3

    def test_bare_httpx_caller_still_identified_by_user_agent(self):
        """With no X-Agent-Name, the httpx UA is still a usable identity."""
        keyed = rate_limit_client_id(_FakeRequest({"User-Agent": "python-httpx/0.27.0"}))
        assert "python-httpx" in keyed


# ── No bypass for unidentified callers ─────────────────────────────


class TestUnidentifiedCallersAreStillLimited:
    def test_no_headers_falls_back_to_ip(self):
        keyed = rate_limit_client_id(_FakeRequest({}, host="10.0.0.9"))
        assert keyed
        assert "10.0.0.9" in keyed

    def test_blank_agent_name_falls_back_to_ip(self):
        """An empty/whitespace header must not yield an empty key that every
        anonymous caller would share *and* that reads as "no limit"."""
        keyed = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "   "}, host="10.0.0.9"))
        assert keyed == rate_limit_client_id(_FakeRequest({}, host="10.0.0.9"))

    def test_punctuation_only_agent_name_falls_back_to_ip(self):
        """Sanitizing "!!!" to "" must fall through to the IP, not produce a
        blank key."""
        keyed = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "!!!"}, host="10.0.0.9"))
        assert "10.0.0.9" in keyed

    def test_missing_client_still_yields_a_key(self):
        """No peer address at all must still be limited, not unkeyed."""
        keyed = rate_limit_client_id(_FakeRequest({}, host=None))
        assert keyed

    def test_anonymous_callers_share_one_ip_bucket(self):
        """Two unidentified callers from one IP share a budget — that is the
        intended conservative behaviour, not a bug."""
        a = rate_limit_client_id(_FakeRequest({}, host="127.0.0.1"))
        b = rate_limit_client_id(_FakeRequest({}, host="127.0.0.1"))
        assert a == b


# ── Hostile headers must not blow up the bucket dict ───────────────


class TestHostileAgentNamesAreBounded:
    """``RateLimitMiddleware._buckets`` is never evicted, so an attacker-
    controlled key space is a memory-growth vector."""

    def test_very_long_name_is_truncated(self):
        keyed = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "z" * 10_000}))
        assert len(keyed) < 200

    def test_long_names_sharing_a_prefix_collapse_to_one_key(self):
        """1000 distinct 10k-char names must not become 1000 bucket keys."""
        keys = {
            rate_limit_client_id(_FakeRequest({"X-Agent-Name": "z" * MAX_AGENT_NAME_LEN + str(i)}))
            for i in range(1000)
        }
        assert len(keys) == 1

    @pytest.mark.parametrize(
        "hostile",
        [
            "claude\r\nX-Injected: 1",
            "../../etc/passwd",
            "claude; DROP TABLE mem_observations",
            "<script>alert(1)</script>",
            "\x00\x01\x02",
            # Non-ASCII but latin-1 encodable — Starlette rejects anything
            # wider at header-construction time, so this is the realistic
            # worst case that can actually reach us.
            "cläude-ñ",
        ],
    )
    def test_hostile_names_are_sanitized(self, hostile):
        keyed = rate_limit_client_id(_FakeRequest({"X-Agent-Name": hostile}))
        assert keyed
        assert len(keyed) < 200
        # No control characters or CRLF survive into a key we log/store.
        assert "\r" not in keyed and "\n" not in keyed
        assert "\x00" not in keyed

    def test_sanitize_bounds_length(self):
        assert len(sanitize_agent_name("a" * 5000)) <= MAX_AGENT_NAME_LEN

    def test_sanitize_rejects_unusable_input(self):
        assert sanitize_agent_name("") == ""
        assert sanitize_agent_name(None) == ""
        assert sanitize_agent_name("   ") == ""
        assert sanitize_agent_name("###") == ""

    def test_hostile_name_cannot_impersonate_a_trusted_agent_bucket(self):
        """A garbage name must get its OWN bucket, not a trusted agent's, and
        must not escape limiting."""
        keyed = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "totally-made-up-agent"}))
        assert keyed
        assert keyed != rate_limit_client_id(_FakeRequest({"X-Agent-Name": "claude"}))


# ── Auth path must not regress ─────────────────────────────────────


class TestAuthenticatedTokenPath:
    def test_token_agent_name_is_used(self):
        keyed = rate_limit_client_id(_FakeRequest(token={"agent_name": "svc-a"}))
        assert "svc-a" in keyed

    def test_token_wins_over_headers(self):
        """A validated token is stronger evidence than a spoofable header."""
        with_hdr = rate_limit_client_id(
            _FakeRequest({"X-Agent-Name": "claude"}, token={"agent_name": "svc-a"})
        )
        without = rate_limit_client_id(_FakeRequest(token={"agent_name": "svc-a"}))
        assert with_hdr == without

    def test_distinct_tokens_get_distinct_buckets(self):
        a = rate_limit_client_id(_FakeRequest(token={"agent_name": "svc-a"}))
        b = rate_limit_client_id(_FakeRequest(token={"agent_name": "svc-b"}))
        assert a != b

    def test_token_identity_does_not_collide_with_header_identity(self):
        """An unauthenticated caller claiming X-Agent-Name: svc-a must not land
        in the authenticated svc-a bucket."""
        tokened = rate_limit_client_id(_FakeRequest(token={"agent_name": "svc-a"}))
        spoofed = rate_limit_client_id(_FakeRequest({"X-Agent-Name": "svc-a"}))
        assert tokened != spoofed


# ── Identity resolution shared with auth ───────────────────────────


class TestLimitScopesAreIndependent:
    """The bucket key must include the limit class, not just the method.

    Second bug found while verifying #63: limits vary by path (admin 10/min,
    /queue 300/min, writes/reads from settings) but the key was only
    ``{client}:{method}``. So an /api/admin GET (cap 10) and an ordinary read
    (cap 500) shared a bucket, and whichever arrived first fixed its capacity —
    11 admin calls would then 429 every plain read for that caller.
    """

    def test_admin_and_read_get_are_different_scopes(self):
        admin, _, admin_cap = rate_limit_scope("GET", "/api/admin/stats")
        read, _, read_cap = rate_limit_scope("GET", "/api/observations")
        assert admin != read
        assert admin_cap == 10
        assert read_cap == settings.rate_limit_reads_per_min

    def test_queue_and_plain_write_are_different_scopes(self):
        queue, _, queue_cap = rate_limit_scope("POST", "/api/queue")
        write, _, write_cap = rate_limit_scope("POST", "/api/observations")
        assert queue != write
        assert queue_cap == 300
        assert write_cap == settings.rate_limit_writes_per_min

    def test_existing_limits_are_unchanged(self):
        """#63 must not alter the documented per-scope limits."""
        assert rate_limit_scope("GET", "/api/admin/stats")[2] == 10
        assert rate_limit_scope("POST", "/api/admin/re-embed")[2] == 10
        assert rate_limit_scope("POST", "/api/queue")[2] == 300
        assert rate_limit_scope("PATCH", "/api/lessons/1")[2] == (
            settings.rate_limit_writes_per_min
        )
        assert rate_limit_scope("DELETE", "/api/observations/1")[2] == (
            settings.rate_limit_writes_per_min
        )
        assert rate_limit_scope("GET", "/api/observations")[2] == (
            settings.rate_limit_reads_per_min
        )

    def test_admin_capacity_does_not_leak_into_reads(self):
        """End-to-end over the real middleware object: exhaust the cap-10 admin
        bucket, then confirm an ordinary read still has its own budget."""
        from app.middleware import RateLimitMiddleware

        mw = RateLimitMiddleware(app=None)
        for _ in range(20):
            mw._get_bucket("agent:x:admin:GET", 10.0 / 60, 10).consume()
        assert mw._get_bucket("agent:x:admin:GET", 10.0 / 60, 10).consume() is False
        # A read bucket for the same caller must be untouched.
        assert mw._get_bucket("agent:x:read:GET", 500.0 / 60, 500).consume() is True


class TestResolveAgentIdentity:
    def test_trusted_name_normalizes_to_configured_value(self):
        """Substring match against the trusted list returns the *configured*
        name, keeping the identity space closed over a known-small set."""
        assert resolve_agent_identity(_FakeRequest({"User-Agent": "python-httpx/0.27"})) == (
            "python-httpx"
        )

    def test_unknown_name_is_preserved_but_sanitized(self):
        assert resolve_agent_identity(_FakeRequest({"X-Agent-Name": "My Bot!"})) == "my-bot"

    def test_no_headers_yields_empty(self):
        assert resolve_agent_identity(_FakeRequest({})) == ""
