"""
Redis-backed rate limiter for FastAPI services.

Implements a **fixed-window counter** strategy:

  Key format:  ``rate_limit:{service}:{client_ip}:{window_id}``
  Window ID:   ``floor(unix_timestamp / window_seconds)``
  Atomicity:   Redis pipeline (INCR + EXPIRE in one round-trip)

Why fixed-window?
  Simple, predictable, and O(1) in both time and memory per counter.
  The main trade-off (burst at window boundary) is acceptable for this
  use-case and can be tightened later by switching to sliding-window if
  needed.

Graceful degradation:
  If Redis is unavailable (startup, transient failure), every request is
  allowed through and a warning is emitted.  The rate limiter will
  automatically re-attempt the connection on the next request.

Thread-safety:
  Uses ``redis.asyncio`` so all Redis I/O is non-blocking and compatible
  with FastAPI's async event loop.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import JSONResponse

from common.constants import (
    RATE_LIMIT_AUTH_LIMIT,
    RATE_LIMIT_AUTH_WINDOW,
    RATE_LIMIT_DEFAULT_LIMIT,
    RATE_LIMIT_DEFAULT_WINDOW,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PORT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EndpointLimit:
    """Rate limit rule for a specific URL path prefix.

    Attributes:
        path_prefix: URL prefix to match (e.g. ``"/v1/auth/"``).
            The first matching rule in ``RateLimitConfig.endpoint_limits``
            is applied; ordering matters.
        limit:  Maximum number of requests allowed in *window* seconds.
        window: Window duration in seconds.
    """

    path_prefix: str
    limit: int
    window: int


@dataclass
class RateLimitConfig:
    """Configuration for the rate limiting middleware.

    Attributes:
        default_limit:    Global request cap (per ``default_window``).
        default_window:   Global window size in seconds.
        endpoint_limits:  Per-prefix overrides evaluated before the global
                          default.  First match wins.
        exempt_paths:     Exact paths that bypass rate limiting entirely
                          (monitoring, docs, CORS preflights, etc.).
        include_headers:  Attach ``X-RateLimit-*`` headers to every response.
    """

    default_limit: int = RATE_LIMIT_DEFAULT_LIMIT
    default_window: int = RATE_LIMIT_DEFAULT_WINDOW
    endpoint_limits: List[EndpointLimit] = field(default_factory=list)
    exempt_paths: List[str] = field(
        default_factory=lambda: [
            "/health",
            "/metrics",
            "/docs",
            "/redoc",
            "/openapi.json",
        ]
    )
    include_headers: bool = True


def default_rate_limit_config() -> RateLimitConfig:
    """Return a ``RateLimitConfig`` with sensible security defaults.

    Applied limits:
      - Auth endpoints (``/v1/auth/``, ``/v1/users/register``):
        ``RATE_LIMIT_AUTH_LIMIT`` req / ``RATE_LIMIT_AUTH_WINDOW`` s
        to mitigate brute-force / credential-stuffing attacks.
      - All other endpoints: ``RATE_LIMIT_DEFAULT_LIMIT`` req / 60 s.
    """
    return RateLimitConfig(
        endpoint_limits=[
            EndpointLimit(
                path_prefix="/v1/auth/",
                limit=RATE_LIMIT_AUTH_LIMIT,
                window=RATE_LIMIT_AUTH_WINDOW,
            ),
            EndpointLimit(
                path_prefix="/v1/users/register",
                limit=RATE_LIMIT_AUTH_LIMIT,
                window=RATE_LIMIT_AUTH_WINDOW,
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Core rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Async Redis-backed rate limiter using a fixed-window counter strategy.

    One counter per (service, client_ip, window_id) is stored in Redis.
    Counters expire automatically after the window closes.

    Thread-safety: uses ``redis.asyncio`` so I/O never blocks the event loop.
    Graceful degradation: if Redis is unreachable the limiter allows every
    request and logs a warning rather than hard-failing.
    """

    def __init__(self, config: RateLimitConfig, service_name: str) -> None:
        self.config = config
        self.service_name = service_name
        self._redis: Optional[aioredis.Redis] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_redis(self) -> Optional[aioredis.Redis]:
        """Return a healthy async Redis connection, or ``None`` if unavailable."""
        if self._redis is not None:
            return self._redis

        password: Optional[str] = os.getenv("REDIS_PASSWORD") or None
        try:
            client = aioredis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                password=password,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await client.ping()
            self._redis = client
        except Exception as exc:
            logger.warning(
                "RateLimiter: Redis unavailable (%s). "
                "Rate limiting is disabled until Redis recovers.",
                exc,
            )
            self._redis = None

        return self._redis

    def _get_limit_and_window(self, path: str) -> tuple[int, int]:
        """Return ``(limit, window_seconds)`` for *path*.

        Iterates ``endpoint_limits`` in declaration order and returns the
        first matching rule.  Falls back to the global defaults when no
        prefix matches.
        """
        for rule in self.config.endpoint_limits:
            if path.startswith(rule.path_prefix):
                return rule.limit, rule.window
        return self.config.default_limit, self.config.default_window

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        """Extract the real client IP address.

        Honours ``X-Forwarded-For`` when the service is behind a reverse
        proxy or load balancer (takes the leftmost, i.e. original, IP).
        Falls back to the direct TCP peer address.
        """
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def check(self, request: Request) -> Optional[JSONResponse]:
        """Evaluate the rate limit for *request*.

        When the request is within the limit, the method stores rate-limit
        metadata in ``request.state.rate_limit_headers`` so the calling
        middleware can forward those headers to the actual response.

        Returns:
            ``None``               — request is within the limit; proceed.
            ``JSONResponse(429)``  — limit exceeded; abort immediately.
        """
        path = request.url.path

        # CORS preflight requests must never be blocked.
        if request.method == "OPTIONS":
            return None

        # Configured exempt paths are never rate-limited.
        if path in self.config.exempt_paths:
            return None

        r = await self._get_redis()
        if r is None:
            # Redis unavailable: allow the request (graceful degradation).
            return None

        client_ip = self._get_client_ip(request)
        limit, window = self._get_limit_and_window(path)

        # Fixed-window: the window_id increments every `window` seconds.
        window_id = int(time.time()) // window
        key = f"rate_limit:{self.service_name}:{client_ip}:{window_id}"
        reset_at = (window_id + 1) * window  # Unix timestamp of next reset

        try:
            # Execute INCR and EXPIRE atomically via a pipeline.
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, window)
            results = await pipe.execute()
            current = int(results[0])
        except Exception as exc:
            # Transient Redis error: allow the request, reset the cached
            # connection so it will be re-established on the next call.
            logger.warning(
                "RateLimiter: Redis pipeline error (%s). Allowing request.",
                exc,
            )
            self._redis = None
            return None

        remaining = max(0, limit - current)

        headers: dict[str, str] = {}
        if self.config.include_headers:
            headers = {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "X-RateLimit-Window": str(window),
            }

        # Persist headers so the middleware can attach them to the response.
        request.state.rate_limit_headers = headers

        if current > limit:
            retry_after = max(1, reset_at - int(time.time()))
            headers["Retry-After"] = str(retry_after)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "detail": (
                        f"Too many requests. "
                        f"Limit: {limit} per {window}s. "
                        f"Retry after {retry_after}s."
                    ),
                },
                headers=headers,
            )

        return None

    async def close(self) -> None:
        """Release the underlying Redis connection pool.

        Should be called on application shutdown (e.g. registered as a
        FastAPI ``shutdown`` event handler).
        """
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
