"""
Tests for Auth0 token verification module.

verify_token now uses /userinfo introspection instead of local JWT parsing,
so tests mock httpx.AsyncClient rather than JWKS/PyJWT internals.

These are UNIT tests — they use mocked Auth0 endpoints.
"""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from libs.auth.auth0_verify import verify_token, _cache, _cache_lock

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _clear_cache():
    with _cache_lock:
        _cache.clear()


def _mock_userinfo(status_code: int, json_body: dict | None = None):
    """Return an async context-manager mock for httpx.AsyncClient that
    returns a fixed response from GET /userinfo."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_body or {}

    client_mock = AsyncMock()
    client_mock.get = AsyncMock(return_value=response)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock


def _mock_userinfo_raises(exc):
    """Return an async context-manager mock whose GET raises exc."""
    client_mock = AsyncMock()
    client_mock.get = AsyncMock(side_effect=exc)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_valid_token_returns_payload():
    """A 200 from /userinfo returns the claims dict."""
    _clear_cache()
    client = _mock_userinfo(200, {"sub": "auth0|abc123", "email": "u@example.com"})

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        payload = await verify_token(credentials=_creds("valid-token"))

    assert payload["sub"] == "auth0|abc123"
    assert payload["email"] == "u@example.com"


@pytest.mark.asyncio
async def test_verify_valid_token_is_cached():
    """Second call with same token uses cache — only one /userinfo request made."""
    _clear_cache()
    client = _mock_userinfo(200, {"sub": "auth0|cached"})

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        await verify_token(credentials=_creds("cached-token"))
        await verify_token(credentials=_creds("cached-token"))

    # Only one real HTTP call despite two verify_token calls
    assert client.get.call_count == 1


# ---------------------------------------------------------------------------
# 401 cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_invalid_token_returns_401():
    """Auth0 returning 401 propagates as 401."""
    _clear_cache()
    client = _mock_userinfo(401, {"error": "invalid_token"})

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("bad-token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or expired token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_auth0_non_200_non_401_returns_401():
    """Any non-200 response (e.g. 500) from Auth0 surfaces as 401."""
    _clear_cache()
    client = _mock_userinfo(500)

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("some-token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "500" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_missing_sub_returns_401():
    """/userinfo response without 'sub' is rejected."""
    _clear_cache()
    client = _mock_userinfo(200, {"email": "u@example.com"})

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("no-sub-token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "sub" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Network error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_timeout_returns_401():
    """A timeout reaching Auth0 surfaces as 401."""
    _clear_cache()
    client = _mock_userinfo_raises(httpx.TimeoutException("timed out"))

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("slow-token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "timed out" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_verify_connection_error_returns_401():
    """A network error reaching Auth0 surfaces as 401."""
    _clear_cache()
    client = _mock_userinfo_raises(httpx.ConnectError("connection refused"))

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("unreachable-token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
