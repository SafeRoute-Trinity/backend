"""
Auth0 verification module for FastAPI.

Verifies Auth0 id_tokens via JWKS (RS256) using PyJWT's PyJWKClient, which
handles key fetching, kid-based key selection, and signature verification
reliably against Auth0's key format.

The mobile app sends the id_token (not the access_token) as the Bearer token
because access_tokens issued for the Management API audience cannot be used
with /userinfo. The id_token is always a verifiable RS256 JWT.

Use verify_token as a FastAPI dependency to protect routes.

Environment variables:
    AUTH0_DOMAIN:     Auth0 domain (e.g., saferouteapp.eu.auth0.com)
    AUTH0_CLIENT_ID:  Auth0 application client ID
"""

import os
import threading
import time

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer
from jwt import ExpiredSignatureError, InvalidTokenError, PyJWKClient

from common.constants import AUTH0_DOMAIN

# Security scheme
security = HTTPBearer()

AUTH0_CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "ZHAiPyzoAyaaiKM0do7J05YNUrLgXFcG")
JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
ISSUER = f"https://{AUTH0_DOMAIN}/"

# PyJWT's PyJWKClient handles key caching internally (lifespan=300s by default)
_jwks_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)

# ---------------------------------------------------------------------------
# Simple in-process token cache
# Avoids a JWKS round-trip on every request for the same token.
# ---------------------------------------------------------------------------
_CACHE_TTL = int(os.getenv("AUTH0_TOKEN_CACHE_TTL", "300"))  # 5 minutes default
_cache: dict[str, tuple[dict, float]] = {}  # token -> (payload, expiry_ts)
_cache_lock = threading.Lock()

# JWKS cache — keys rotate rarely so cache for 1 hour
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
_JWKS_CACHE_TTL = 3600


def _cache_get(token: str) -> dict | None:
    with _cache_lock:
        entry = _cache.get(token)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _cache[token]
    return None


def _cache_set(token: str, payload: dict) -> None:
    with _cache_lock:
        _cache[token] = (payload, time.monotonic() + _CACHE_TTL)


async def _get_jwks() -> dict:
    """Fetch JWKS from Auth0, with a 1-hour in-process cache."""
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache and (now - _jwks_fetched_at) < _JWKS_CACHE_TTL:
        return _jwks_cache
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(JWKS_URL)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Failed to fetch Auth0 JWKS: {resp.status_code}",
        )
    _jwks_cache = resp.json()
    _jwks_fetched_at = now
    return _jwks_cache


# ---------------------------------------------------------------------------
# verify_token dependency
# ---------------------------------------------------------------------------


async def verify_token(
    credentials=Depends(security),
) -> dict:
    """
    Verify an Auth0 id_token via JWKS RS256 signature verification.

    Uses PyJWT's PyJWKClient to fetch Auth0's public keys and verify the
    id_token signature. Works reliably regardless of access_token audience.

    Returns a dict with at least {"sub": "<auth0-user-id>", ...}.

    Raises:
        HTTPException 401: if the token is invalid, expired, or cannot be
                           verified against Auth0's public keys.
    """
    token = credentials.credentials

    # Fast path: cache hit
    cached = _cache_get(token)
    if cached:
        return cached

    # Fetch the signing key for this token's kid
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: could not fetch signing key: {e}",
        ) from e

    # Verify and decode the JWT
    try:
        payload: dict = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=AUTH0_CLIENT_ID,
            issuer=ISSUER,
        )
    except ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        ) from e
    except InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e

    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed: missing sub claim",
        )

    print(f"[Auth0] ID token verified for user: {payload.get('sub')}")
    _cache_set(token, payload)
    return payload


# ---------------------------------------------------------------------------
# Router for Auth0 verification endpoints
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/auth0", tags=["auth"])


@router.get("/verify")
async def verify(payload: dict = Depends(verify_token)):
    """
    Protected endpoint that returns user info if token is valid.

    Returns:
        Dict containing validation message and user ID from token
    """
    return {"message": "Token valid", "user": payload.get("sub")}


# Minimal standalone app for local testing
if os.getenv("AUTH0_STANDALONE", "0") == "1":
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
