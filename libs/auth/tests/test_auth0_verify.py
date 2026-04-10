"""
Tests for Auth0 token verification module.

verify_token uses PyJWT's PyJWKClient for RS256 id_token verification.
Tests mock _jwks_client.get_signing_key_from_jwt and pyjwt.decode.

These are UNIT tests — they do not hit real Auth0 endpoints.
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from jwt import ExpiredSignatureError, InvalidTokenError

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


def _mock_signing_key():
    key = MagicMock()
    key.key = "mock-public-key"
    return key


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_valid_token_returns_payload():
    """A valid id_token returns the decoded claims dict."""
    _clear_cache()
    decoded = {"sub": "auth0|abc123", "email": "u@example.com"}

    with patch("libs.auth.auth0_verify._jwks_client.get_signing_key_from_jwt", return_value=_mock_signing_key()):
        with patch("libs.auth.auth0_verify.pyjwt.decode", return_value=decoded):
            payload = await verify_token(credentials=_creds("valid.jwt.token"))

    assert payload["sub"] == "auth0|abc123"
    assert payload["email"] == "u@example.com"


@pytest.mark.asyncio
async def test_verify_valid_token_is_cached():
    """Second call with same token uses cache — only one JWKS lookup."""
    _clear_cache()
    decoded = {"sub": "auth0|cached", "email": "c@example.com"}
    mock_get_key = MagicMock(return_value=_mock_signing_key())

    with patch("libs.auth.auth0_verify._jwks_client.get_signing_key_from_jwt", mock_get_key):
        with patch("libs.auth.auth0_verify.pyjwt.decode", return_value=decoded):
            await verify_token(credentials=_creds("cached.jwt.token"))
            await verify_token(credentials=_creds("cached.jwt.token"))

    # Only one JWKS lookup despite two verify_token calls (second hits cache)
    assert mock_get_key.call_count == 1


# ---------------------------------------------------------------------------
# 401 cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_invalid_token_returns_401():
    """An invalid JWT raises 401."""
    _clear_cache()

    with patch("libs.auth.auth0_verify._jwks_client.get_signing_key_from_jwt", return_value=_mock_signing_key()):
        with patch(
            "libs.auth.auth0_verify.pyjwt.decode",
            side_effect=InvalidTokenError("Signature verification failed"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=_creds("bad.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_expired_token_returns_401():
    """An expired JWT raises 401."""
    _clear_cache()

    with patch("libs.auth.auth0_verify._jwks_client.get_signing_key_from_jwt", return_value=_mock_signing_key()):
        with patch(
            "libs.auth.auth0_verify.pyjwt.decode",
            side_effect=ExpiredSignatureError("Token is expired"),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=_creds("expired.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "expired" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_verify_missing_sub_returns_401():
    """A JWT without 'sub' is rejected."""
    _clear_cache()
    decoded = {"email": "u@example.com"}  # No sub

    with patch("libs.auth.auth0_verify._jwks_client.get_signing_key_from_jwt", return_value=_mock_signing_key()):
        with patch("libs.auth.auth0_verify.pyjwt.decode", return_value=decoded):
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=_creds("no-sub.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "sub" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_jwks_fetch_failure_returns_401():
    """Failure to fetch the signing key (network error, bad kid) surfaces as 401."""
    _clear_cache()

    with patch(
        "libs.auth.auth0_verify._jwks_client.get_signing_key_from_jwt",
        side_effect=Exception("Unable to find a signing key"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=_creds("unknown-kid.jwt.token"))

    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "signing key" in exc_info.value.detail
