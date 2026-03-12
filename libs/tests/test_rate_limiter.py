"""
Unit tests for libs/rate_limiter.py

Strategy
--------
All tests go through FastAPI's ``TestClient`` so that the async middleware
runs inside a real (but in-process) ASGI call cycle.  Redis is always mocked
via ``pytest-mock`` ``AsyncMock`` objects – no real Redis instance is needed.

Test matrix
-----------
1.  Requests under the limit are allowed (200) with X-RateLimit-* headers.
2.  The first request that *exceeds* the limit is rejected with 429.
3.  Exempt paths (/health, /metrics, /docs, /redoc, /openapi.json) bypass
    the limiter entirely.
4.  CORS preflight (OPTIONS) requests always pass through.
5.  Per-endpoint overrides are respected (auth endpoints are stricter).
6.  Graceful degradation: Redis unavailable → requests allowed, no crash.
7.  Graceful degradation: Redis pipeline error → requests allowed.
8.  ``X-Forwarded-For`` header is used as the rate-limit key when present.
9.  ``RateLimiter.close()`` releases the Redis connection.
10. ``default_rate_limit_config`` includes auth endpoint overrides.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libs.rate_limiter import (
    EndpointLimit,
    RateLimitConfig,
    RateLimiter,
    default_rate_limit_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(limiter: RateLimiter) -> FastAPI:
    """Build a minimal FastAPI app wired with *limiter* as middleware."""
    app = FastAPI()

    @app.middleware("http")
    async def _rl_middleware(request, call_next):
        rejection = await limiter.check(request)
        if rejection is not None:
            return rejection
        response = await call_next(request)
        for k, v in getattr(request.state, "rate_limit_headers", {}).items():
            response.headers[k] = v
        return response

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return {"metrics": "ok"}

    @app.get("/v1/auth/login")
    async def login():
        return {"token": "abc"}

    return app


def _mock_pipeline(count: int) -> MagicMock:
    """Return a mock Redis pipeline whose execute() yields *count* as INCR result."""
    pipe = MagicMock()
    pipe.incr = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.execute = AsyncMock(return_value=[count, 1])
    return pipe


def _make_redis_mock(count: int) -> AsyncMock:
    """Return a mock ``redis.asyncio.Redis`` whose INCR counter is *count*."""
    redis_mock = AsyncMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.pipeline = MagicMock(return_value=_mock_pipeline(count))
    redis_mock.aclose = AsyncMock(return_value=None)
    return redis_mock


# ---------------------------------------------------------------------------
# Tests – allowed requests
# ---------------------------------------------------------------------------


def test_request_under_limit_is_allowed():
    """A request whose counter is below the limit receives 200."""
    config = RateLimitConfig(default_limit=10, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=5)  # 5 < 10 → allowed

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 200


def test_rate_limit_headers_present_on_allowed_request():
    """X-RateLimit-* headers are attached to allowed responses."""
    config = RateLimitConfig(default_limit=10, default_window=60, include_headers=True)
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=3)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "10"
    assert resp.headers["X-RateLimit-Remaining"] == "7"  # 10 - 3
    assert "X-RateLimit-Reset" in resp.headers
    assert resp.headers["X-RateLimit-Window"] == "60"


def test_no_rate_limit_headers_when_disabled():
    """When ``include_headers=False`` no X-RateLimit-* headers are added."""
    config = RateLimitConfig(default_limit=10, default_window=60, include_headers=False)
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=1)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 200
    assert "X-RateLimit-Limit" not in resp.headers


# ---------------------------------------------------------------------------
# Tests – limit exceeded
# ---------------------------------------------------------------------------


def test_request_over_limit_returns_429():
    """A request whose counter exceeds the limit is rejected with 429."""
    config = RateLimitConfig(default_limit=5, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=6)  # 6 > 5 → blocked

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 429


def test_429_response_body_contains_error_field():
    """The 429 JSON body has the ``error`` and ``detail`` fields."""
    config = RateLimitConfig(default_limit=1, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=2)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "rate_limit_exceeded"
    assert "detail" in body


def test_429_response_includes_retry_after_header():
    """Retry-After header is present and positive when rate limited."""
    config = RateLimitConfig(default_limit=1, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=2)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# Tests – exempt paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/metrics", "/docs", "/redoc", "/openapi.json"])
def test_exempt_paths_bypass_rate_limiter(path):
    """Configured exempt paths never reach the Redis counter."""
    config = RateLimitConfig(default_limit=0, default_window=60)  # limit=0 → every hit blocked
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=999)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get(path)

    # The limiter must not return 429 for exempt paths, regardless of counter.
    assert resp.status_code != 429


# ---------------------------------------------------------------------------
# Tests – CORS preflight passthrough
# ---------------------------------------------------------------------------


def test_options_preflight_is_never_rate_limited():
    """HTTP OPTIONS requests always pass through regardless of counter."""
    config = RateLimitConfig(default_limit=0, default_window=60)  # all blocked
    limiter = RateLimiter(config, "test_svc")

    redis_mock = _make_redis_mock(count=999)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.options("/ping")

    assert resp.status_code != 429


# ---------------------------------------------------------------------------
# Tests – per-endpoint overrides
# ---------------------------------------------------------------------------


def test_endpoint_override_uses_stricter_limit():
    """Auth endpoints use a lower limit than the global default."""
    config = RateLimitConfig(
        default_limit=100,
        default_window=60,
        endpoint_limits=[
            EndpointLimit(path_prefix="/v1/auth/", limit=3, window=60),
        ],
    )
    limiter = RateLimiter(config, "test_svc")

    # counter = 4, which exceeds the auth limit (3) but not the global (100)
    redis_mock = _make_redis_mock(count=4)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/v1/auth/login")

    assert resp.status_code == 429


def test_endpoint_override_does_not_affect_other_paths():
    """A strict auth limit must not apply to non-auth endpoints."""
    config = RateLimitConfig(
        default_limit=100,
        default_window=60,
        endpoint_limits=[
            EndpointLimit(path_prefix="/v1/auth/", limit=3, window=60),
        ],
    )
    limiter = RateLimiter(config, "test_svc")

    # counter = 4: blocked for auth, but fine for /ping (limit=100)
    redis_mock = _make_redis_mock(count=4)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 200


def test_first_matching_endpoint_limit_wins():
    """When multiple rules could match, the first one in the list is used."""
    config = RateLimitConfig(
        default_limit=100,
        default_window=60,
        endpoint_limits=[
            EndpointLimit(path_prefix="/v1/", limit=5, window=60),  # first
            EndpointLimit(path_prefix="/v1/auth/", limit=2, window=60),  # never reached
        ],
    )
    limiter = RateLimiter(config, "test_svc")

    # counter=3: exceeds second rule (2) but within first rule (5)
    redis_mock = _make_redis_mock(count=3)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/v1/auth/login")

    # First matching rule (/v1/) has limit 5; 3 < 5 → allowed
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests – graceful degradation
# ---------------------------------------------------------------------------


def test_requests_allowed_when_redis_unavailable():
    """If Redis cannot be reached, every request is allowed (no crash)."""
    config = RateLimitConfig(default_limit=1, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=None)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 200


def test_requests_allowed_on_redis_pipeline_error():
    """A transient Redis pipeline error must not crash the service."""
    config = RateLimitConfig(default_limit=5, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    broken_pipe = MagicMock()
    broken_pipe.incr = MagicMock(return_value=None)
    broken_pipe.expire = MagicMock(return_value=None)
    broken_pipe.execute = AsyncMock(side_effect=ConnectionError("Redis gone"))

    redis_mock = AsyncMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.pipeline = MagicMock(return_value=broken_pipe)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        resp = client.get("/ping")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests – X-Forwarded-For
# ---------------------------------------------------------------------------


def test_x_forwarded_for_is_used_as_client_ip():
    """Requests with X-Forwarded-For use the forwarded IP as the rate-limit key."""
    config = RateLimitConfig(default_limit=5, default_window=60)
    limiter = RateLimiter(config, "test_svc")

    captured_keys: list[str] = []

    pipe = MagicMock()
    pipe.incr = MagicMock(side_effect=lambda key: captured_keys.append(key))
    pipe.expire = MagicMock(return_value=None)
    pipe.execute = AsyncMock(return_value=[1, 1])

    redis_mock = AsyncMock()
    redis_mock.ping = AsyncMock(return_value=True)
    redis_mock.pipeline = MagicMock(return_value=pipe)

    with patch.object(limiter, "_get_redis", new=AsyncMock(return_value=redis_mock)):
        client = TestClient(_make_app(limiter))
        client.get("/ping", headers={"X-Forwarded-For": "203.0.113.42, 10.0.0.1"})

    assert any("203.0.113.42" in k for k in captured_keys)


# ---------------------------------------------------------------------------
# Tests – RateLimiter.close()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_releases_redis_connection():
    """close() calls aclose() on the Redis client and clears the reference."""
    config = RateLimitConfig()
    limiter = RateLimiter(config, "test_svc")

    redis_mock = AsyncMock()
    redis_mock.aclose = AsyncMock(return_value=None)
    limiter._redis = redis_mock

    await limiter.close()

    redis_mock.aclose.assert_awaited_once()
    assert limiter._redis is None


@pytest.mark.asyncio
async def test_close_is_idempotent_when_no_connection():
    """close() on a limiter that never connected must not raise."""
    config = RateLimitConfig()
    limiter = RateLimiter(config, "test_svc")
    await limiter.close()  # must not raise


# ---------------------------------------------------------------------------
# Tests – default_rate_limit_config
# ---------------------------------------------------------------------------


def test_default_config_has_auth_endpoint_overrides():
    """default_rate_limit_config() includes rules for auth and register paths."""
    cfg = default_rate_limit_config()
    prefixes = [rule.path_prefix for rule in cfg.endpoint_limits]
    assert "/v1/auth/" in prefixes
    assert "/v1/users/register" in prefixes


def test_default_config_auth_limit_is_stricter_than_default():
    """Auth endpoint limit must be lower than the global default."""
    cfg = default_rate_limit_config()
    auth_rule = next(r for r in cfg.endpoint_limits if r.path_prefix == "/v1/auth/")
    assert auth_rule.limit < cfg.default_limit
