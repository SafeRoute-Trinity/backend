"""
Tests for Auth0 token verification module.

verify_token now uses JWKS-based RS256 verification of the id_token,
so tests mock jose.jwt.decode and the JWKS fetch rather than httpx /userinfo.

These are UNIT tests — they use mocked Auth0 endpoints.
"""

import pytest
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


def _mock_jwks_fetch(status_code: int = 200, keys: list | None = None):
    """Return an async context-manager mock for httpx.AsyncClient that
    returns a fixed JWKS response."""
    import httpx
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = {"keys": keys or [{"kty": "RSA", "kid": "test-key"}]}

    client_mock = AsyncMock()
    client_mock.get = AsyncMock(return_value=response)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_valid_token_returns_payload():
    """A valid id_token returns the decoded claims dict."""
    _clear_cache()
    client = _mock_jwks_fetch()
    decoded = {"sub": "auth0|abc123", "email": "u@example.com", "aud": "client-id"}

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with patch("libs.auth.auth0_verify.jwt.decode", return_value=decoded):
            payload = await verify_token(credentials=_creds("valid.jwt.token"))

    assert payload["sub"] == "auth0|abc123"
    assert payload["email"] == "u@example.com"


@pytest.mark.asyncio
async def test_verify_valid_token_is_cached():
    """Second call with same token uses cache — only one JWKS fetch."""
    _clear_cache()
    client = _mock_jwks_fetch()
    decoded = {"sub": "auth0|cached", "email": "c@example.com"}

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with patch("libs.auth.auth0_verify.jwt.decode", return_value=decoded):
            await verify_token(credentials=_creds("cached.jwt.token"))
            await verify_token(credentials=_creds("cached.jwt.token"))

    # Only one JWKS fetch despite two verify_token calls (second hits token cache)
    assert client.get.call_count <= 1


# ---------------------------------------------------------------------------
# 401 cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_invalid_token_returns_401():
    """An invalid/expired JWT raises 401."""
    _clear_cache()
    from jose import JWTError
    client = _mock_jwks_fetch()

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with patch("libs.auth.auth0_verify.jwt.decode", side_effect=JWTError("Signature verification failed")):
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=_creds("bad.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or expired token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_expired_token_returns_401():
    """An expired JWT raises 401."""
    _clear_cache()
    from jose import ExpiredSignatureError
    client = _mock_jwks_fetch()

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with patch("libs.auth.auth0_verify.jwt.decode", side_effect=ExpiredSignatureError("Token is expired")):
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=_creds("expired.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_verify_missing_sub_returns_401():
    """A JWT without 'sub' is rejected."""
    _clear_cache()
    client = _mock_jwks_fetch()
    decoded = {"email": "u@example.com"}  # No sub

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with patch("libs.auth.auth0_verify.jwt.decode", return_value=decoded):
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=_creds("no-sub.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "sub" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_jwks_fetch_failure_returns_401():
    """A non-200 from JWKS endpoint surfaces as 401."""
    _clear_cache()
    import libs.auth.auth0_verify as mod
    mod._jwks_cache = None
    mod._jwks_fetched_at = 0.0

    client = _mock_jwks_fetch(status_code=500)

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("any.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_verify_jwks_network_error_returns_401():
    """A network error fetching JWKS surfaces as 401."""
    _clear_cache()
    import libs.auth.auth0_verify as mod
    mod._jwks_cache = None
    mod._jwks_fetched_at = 0.0

    import httpx
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("libs.auth.auth0_verify.httpx.AsyncClient", return_value=client):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("any.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
