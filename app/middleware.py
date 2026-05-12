"""Middleware for authentication, rate limiting, and audit logging."""

import logging
import time

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.auth import validate_token
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

        # Identify client
        token = getattr(request.state, "token", None)
        client_id = token["agent_name"] if token else (request.client.host if request.client else "unknown")

        # Choose limits based on method and path
        if request.url.path.startswith("/api/admin"):
            rate, cap = 10.0 / 60, 10
        elif request.method in ("POST", "PATCH", "DELETE"):
            if "/queue" in request.url.path:
                rate, cap = 300.0 / 60, 300
            else:
                rate, cap = settings.rate_limit_writes_per_min / 60, settings.rate_limit_writes_per_min
        else:
            rate, cap = settings.rate_limit_reads_per_min / 60, settings.rate_limit_reads_per_min

        bucket = self._get_bucket(f"{client_id}:{request.method}", rate, cap)
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
