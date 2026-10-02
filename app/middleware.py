"""Middleware for authentication, rate limiting, and audit logging."""

import logging
import time

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.auth import resolve_agent_identity, validate_token
from app.config import settings

logger = logging.getLogger(__name__)

# ── Auth Middleware ──────────────────────────────────────────────────

EXEMPT_PATHS = frozenset({"/api/health", "/docs", "/openapi.json", "/redoc"})


class AuthMiddleware(BaseHTTPMiddleware):
    """Validate API tokens on every request (except exempt paths)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            request.state.token = None
            return await call_next(request)

        try:
            token_record = await validate_token(request)
            request.state.token = token_record
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        except Exception as exc:
            logger.error("Auth middleware error: %s", exc)
            return JSONResponse(status_code=500, content={"detail": "Internal auth error"})

        return await call_next(request)


# ── Rate Limiting ───────────────────────────────────────────────────


class _TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def consume(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def rate_limit_client_id(request: Request) -> str:
    """Derive the rate-limit bucket identity for a request.

    Why this exists (issue #63): the original code did

        token = getattr(request.state, "token", None)
        client_id = token["agent_name"] if token else request.client.host

    but ``request.state.token`` is only ever set by ``AuthMiddleware``, which
    ``app/main.py`` registers *only* when ``settings.require_auth`` is true.
    The default is false, so in practice every localhost caller collapsed into
    one shared "127.0.0.1" bucket: the pytest suite, manual CLI runs, and live
    Claude Code hooks firing during that same run all drew from a single
    100-writes/min budget. The symptom was ~40 pytest failures that were all
    429s and looked like real breakage.

    Resolution order:
      1. An authenticated token's ``agent_name`` (unchanged — do not regress
         the auth path).
      2. The agent identity from ``X-Agent-Name`` / ``User-Agent``, resolved
         through ``app.auth.resolve_agent_identity`` so auth and rate limiting
         agree on who a caller is. That value is already sanitized and length
         bounded, which matters because ``_buckets`` is never evicted — an
         unbounded key space would be a memory-growth vector.
      3. The peer IP. This is the *last* resort, not a bypass: a caller that
         declines to identify itself is still limited, just at IP granularity.
    """
    token = getattr(request.state, "token", None)
    if token:
        return f"token:{token['agent_name']}"

    identity = resolve_agent_identity(request)
    if identity:
        return f"agent:{identity}"

    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def rate_limit_scope(method: str, path: str) -> tuple[str, float, int]:
    """Pick the limit class for a request: ``(scope, rate_per_sec, capacity)``.

    ``scope`` names the limit class and becomes part of the bucket key. That
    part matters: the key used to be ``f"{client_id}:{method}"`` while the
    limits here vary by *path* as well, so every GET from one caller shared a
    single bucket whose capacity was whichever class happened to create it
    first. An ``/api/admin`` GET (cap 10) therefore throttled ordinary reads
    (cap 500) for that caller — which is how the issue #63 test run still
    produced 429s on plain list/export endpoints even after the client identity
    was fixed. Keying by scope keeps each documented limit independent.
    """
    if path.startswith("/api/admin"):
        return "admin", 10.0 / 60, 10
    if method in ("POST", "PATCH", "DELETE"):
        if "/queue" in path:
            return "queue", 300.0 / 60, 300
        return "write", settings.rate_limit_writes_per_min / 60, settings.rate_limit_writes_per_min
    return "read", settings.rate_limit_reads_per_min / 60, settings.rate_limit_reads_per_min


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory token-bucket rate limiter."""

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[str, _TokenBucket] = {}

    def _get_bucket(self, key: str, rate: float, capacity: int) -> _TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = _TokenBucket(rate, capacity)
        return self._buckets[key]

    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # Identify client (see rate_limit_client_id for why this is not just
        # request.client.host)
        client_id = rate_limit_client_id(request)

        scope, rate, cap = rate_limit_scope(request.method, request.url.path)
        bucket = self._get_bucket(f"{client_id}:{scope}:{request.method}", rate, cap)
        if not bucket.consume():
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )

        return await call_next(request)


# ── Audit Logging ───────────────────────────────────────────────────


class AuditMiddleware(BaseHTTPMiddleware):
    """Log API operations to mem_audit_log."""

    async def dispatch(self, request: Request, call_next):
        if settings.audit_log_level == "off" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # Skip reads unless audit_log_level is "all"
        if settings.audit_log_level == "writes_only" and request.method == "GET":
            return await call_next(request)

        start = time.monotonic()
        response: Response = await call_next(request)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        token = getattr(request.state, "token", None)
        agent_name = token["agent_name"] if token else None
        ip = request.client.host if request.client else None

        # Fire-and-forget DB write
        try:
            from app.db import get_pool

            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO mem_audit_log (agent_name, method, path, status_code, response_time_ms, ip_address) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    agent_name,
                    request.method,
                    request.url.path,
                    response.status_code,
                    elapsed_ms,
                    ip,
                )
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)

        return response
