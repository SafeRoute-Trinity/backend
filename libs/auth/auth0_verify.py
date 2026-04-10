"""
Auth0 verification module for FastAPI.

Verifies Auth0 id_tokens via JWKS (RS256) using PyJWT for signature
verification. Fetches JWKS asynchronously via httpx so it doesn't block
the event loop, and constructs the RSA public key directly from the JWK
using PyJWT's RSAAlgorithm.from_jwk for reliable key parsing.

The mobile app sends the id_token (not the access_token) as the Bearer token.

Use verify_token as a FastAPI dependency to protect routes.

Environment variables:
    AUTH0_DOMAIN:     Auth0 domain (e.g., saferouteapp.eu.auth0.com)
    AUTH0_CLIENT_ID:  Auth0 application client ID
"""

import os
import threading
import time

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer
from jwt import ExpiredSignatureError, InvalidTokenError
from jwt.algorithms import RSAAlgorithm

from common.constants import AUTH0_DOMAIN

# Security scheme
security = HTTPBearer()

AUTH0_CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "ZHAiPyzoAyaaiKM0do7J05YNUrLgXFcG")
JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
ISSUER = f"https://{AUTH0_DOMAIN}/"

# ---------------------------------------------------------------------------
# Async JWKS cache — keys rotate rarely so cache for 1 hour
# ---------------------------------------------------------------------------
_jwks: dict | None = None
_jwks_fetched_at: float = 0.0
_JWKS_TTL = 3600.0
_jwks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Token-level cache — avoids repeated JWKS lookups for the same token
# ---------------------------------------------------------------------------
_CACHE_TTL = int(os.getenv("AUTH0_TOKEN_CACHE_TTL", "300"))  # 5 minutes
_cache: dict[str, tuple[dict, float]] = {}
_cache_lock = threading.Lock()


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


async def _fetch_jwks(force: bool = False) -> dict:
    """Fetch JWKS from Auth0 asynchronously, with a 1-hour in-process cache."""
    global _jwks, _jwks_fetched_at
    now = time.monotonic()
    if not force and _jwks and (now - _jwks_fetched_at) < _JWKS_TTL:
        return _jwks
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(JWKS_URL)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: cannot reach Auth0 JWKS: {e}",
        ) from e
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: Auth0 JWKS returned {resp.status_code}",
        )
    with _jwks_lock:
        _jwks = resp.json()
        _jwks_fetched_at = now
    return _jwks


def _find_key(jwks: dict, kid: str):
    """Return the JWK dict whose kid matches, or None."""
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return key_data
    return None


async def _get_signing_key(token: str):
    """
    Extract the signing key for this JWT from Auth0's JWKS.

    On a kid miss, forces a JWKS refresh once in case of key rotation.
    """
    try:
        header = pyjwt.get_unverified_header(token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token format: {e}",
        ) from e

    kid = header.get("kid")
    alg = header.get("alg", "RS256")

    if alg != "RS256":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unsupported token algorithm: {alg} (expected RS256)",
        )

    # Try cached JWKS first
    jwks = await _fetch_jwks()
    key_data = _find_key(jwks, kid)

    if key_data is None:
        # Key not in cache — may have been rotated, force a refresh
        jwks = await _fetch_jwks(force=True)
        key_data = _find_key(jwks, kid)

    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: no matching signing key for kid={kid}",
        )

    return RSAAlgorithm.from_jwk(key_data)


# ---------------------------------------------------------------------------
# verify_token dependency
# ---------------------------------------------------------------------------


async def verify_token(
    credentials=Depends(security),
) -> dict:
    """
    Verify an Auth0 id_token via RS256 JWKS signature verification.

    Fetches Auth0's public keys asynchronously, finds the key matching the
    token's kid header, and verifies the RS256 signature locally.

    Returns a dict with at least {"sub": "<auth0-user-id>", ...}.

    Raises:
        HTTPException 401: if the token is invalid, expired, or unverifiable.
    """
    token = credentials.credentials

    # Fast path: cache hit
    cached = _cache_get(token)
    if cached:
        return cached

    signing_key = await _get_signing_key(token)

    try:
        payload: dict = pyjwt.decode(
            token,
            signing_key,
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
