"""
Auth0 verification module for FastAPI.

Verifies tokens by calling Auth0's /userinfo endpoint (token introspection).
This approach works with both opaque tokens and JWTs regardless of audience,
which is required because the mobile app uses the password grant without a
custom API audience.

Use verify_token as a FastAPI dependency to protect routes.

Environment variables:
    AUTH0_DOMAIN: Auth0 domain (e.g., saferouteapp.eu.auth0.com)
"""

import os
import threading
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer

from common.constants import AUTH0_DOMAIN

# Security scheme
security = HTTPBearer()

# ---------------------------------------------------------------------------
# Simple in-process token cache
# Avoids a /userinfo round-trip on every request for the same token.
# Entries expire after CACHE_TTL seconds.
# ---------------------------------------------------------------------------
_CACHE_TTL = int(os.getenv("AUTH0_TOKEN_CACHE_TTL", "300"))  # 5 minutes default
_cache: dict[str, tuple[dict, float]] = {}  # token -> (payload, expiry_ts)
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


# ---------------------------------------------------------------------------
# verify_token dependency
# ---------------------------------------------------------------------------


async def verify_token(
    credentials=Depends(security),
) -> dict:
    """
    Verify an Auth0 access token via /userinfo introspection.

    Works with any token type (opaque or JWT) issued by Auth0, so no specific
    API audience is required on the client side.

    Returns a dict with at least {"sub": "<auth0-user-id>", ...}.

    Raises:
        HTTPException 401: if Auth0 rejects the token or the call fails.
    """
    token = credentials.credentials

    # Fast path: cache hit
    cached = _cache_get(token)
    if cached:
        return cached

    userinfo_url = f"https://{AUTH0_DOMAIN}/userinfo"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                userinfo_url,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.TimeoutException as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification timed out",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {e}",
        ) from e

    if resp.status_code == 401:
        try:
            body = resp.json()
            auth0_error = f"{body.get('error', '')} {body.get('error_description', '')}".strip()
        except Exception:
            auth0_error = resp.text[:200]
        print(f"[Auth0] /userinfo returned 401: {auth0_error}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {auth0_error}",
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: Auth0 returned {resp.status_code}",
        )

    payload: dict = resp.json()
    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed: missing sub claim",
        )

    print(f"[Auth0] Token verified via /userinfo for user: {payload.get('sub')}")
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
