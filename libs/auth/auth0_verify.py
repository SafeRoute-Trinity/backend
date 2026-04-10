"""
Auth0 verification module for FastAPI.

Verifies Auth0 JWTs (id_token or API access_token) via JWKS (RS256) using PyJWT.
JWKS is fetched from the token's ``iss`` claim (``{iss}/.well-known/jwks.json``)
so verification works when the app and server use the same logical tenant but
different hostnames (e.g. typo / legacy domain vs current Auth0 domain), as long
as the issuer is allowlisted.

The mobile app may send an id_token (aud = client_id) or an access_token
(aud = API identifier such as the Management API).

Use verify_token as a FastAPI dependency to protect routes.

Environment variables:
    AUTH0_DOMAIN:     Primary Auth0 domain (e.g. saferouteapp.eu.auth0.com); used
                      for the default trusted issuer and fallbacks.
    AUTH0_CLIENT_ID:  Auth0 application client ID
    AUTH0_ADDITIONAL_ISSUERS: Optional comma-separated issuer URLs (https://...)
                      to trust in addition to https://{AUTH0_DOMAIN}/
    AUTH0_ADDITIONAL_AUDIENCES: Optional comma-separated extra valid ``aud`` values
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer
from jwt import ExpiredSignatureError, InvalidTokenError
from jwt.algorithms import RSAAlgorithm

from common.constants import API_AUDIENCE, AUTH0_CLIENT_ID, AUTH0_DOMAIN

# Security scheme
security = HTTPBearer()

# ---------------------------------------------------------------------------
# Per-issuer JWKS cache — keys rotate rarely so cache for 1 hour per issuer
# ---------------------------------------------------------------------------
_jwks_by_issuer: dict[str, tuple[dict, float]] = {}
_JWKS_TTL = 3600.0
_jwks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Token-level cache — avoids repeated JWKS lookups for the same token
# ---------------------------------------------------------------------------
_CACHE_TTL = int(os.getenv("AUTH0_TOKEN_CACHE_TTL", "300"))  # 5 minutes
_cache: dict[str, tuple[dict, float]] = {}
_cache_lock = threading.Lock()


def _canonical_issuer(iss: str) -> str:
    """Normalize issuer URL for host equality (scheme + host, lowercase, no path)."""
    raw = (iss or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.lower().rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _trusted_canonical_issuers() -> set[str]:
    trusted = {_canonical_issuer(f"https://{AUTH0_DOMAIN}")}
    extra = os.getenv("AUTH0_ADDITIONAL_ISSUERS", "")
    for part in extra.split(","):
        part = part.strip()
        if part:
            trusted.add(_canonical_issuer(part))
    trusted.discard("")
    return trusted


def _jwks_url_for_canonical_issuer(canon: str) -> str:
    return f"{canon}/.well-known/jwks.json"


def _allowed_audiences() -> list[str]:
    auds: list[str] = []
    if AUTH0_CLIENT_ID:
        auds.append(AUTH0_CLIENT_ID)
    if API_AUDIENCE:
        auds.append(API_AUDIENCE)
        stripped = API_AUDIENCE.rstrip("/")
        if stripped != API_AUDIENCE:
            auds.append(stripped)
        elif not API_AUDIENCE.endswith("/"):
            auds.append(API_AUDIENCE + "/")
    # ROPG / native login without a custom API audience: access_token JWTs are often
    # minted for Auth0's OIDC userinfo endpoint (mobile client uses id_token || access_token).
    if AUTH0_DOMAIN:
        userinfo_root = f"{_canonical_issuer(f'https://{AUTH0_DOMAIN}')}/userinfo"
        auds.append(userinfo_root)
        auds.append(userinfo_root + "/")
    for part in os.getenv("AUTH0_ADDITIONAL_AUDIENCES", "").split(","):
        part = part.strip()
        if part:
            auds.append(part)
    seen: set[str] = set()
    out: list[str] = []
    for a in auds:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def reset_auth0_verify_caches() -> None:
    """Clear JWKS and token verification caches (tests)."""
    with _jwks_lock:
        _jwks_by_issuer.clear()
    with _cache_lock:
        _cache.clear()


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


async def _fetch_jwks_for_canonical_issuer(canon: str, force: bool = False) -> dict:
    """Fetch JWKS for an Auth0 tenant (canonical issuer host), with per-issuer cache."""
    now = time.monotonic()
    with _jwks_lock:
        entry = _jwks_by_issuer.get(canon)
        if not force and entry and (now - entry[1]) < _JWKS_TTL:
            return entry[0]

    url = _jwks_url_for_canonical_issuer(canon)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
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
    jwks = resp.json()
    with _jwks_lock:
        _jwks_by_issuer[canon] = (jwks, now)
    return jwks


def _find_key(jwks: dict, kid: str):
    """Return the JWK dict whose kid matches, or None."""
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            return key_data
    return None


async def _get_signing_key_and_issuer(token: str) -> tuple[object, str]:
    """
    Resolve RS256 signing key and return (key, issuer string from token).

    On a kid miss, forces a JWKS refresh once for that issuer (key rotation).
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

    try:
        unverified_payload = pyjwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token claims: {e}",
        ) from e

    token_iss = unverified_payload.get("iss")
    if not token_iss or not isinstance(token_iss, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token verification failed: missing or invalid iss claim",
        )

    canon_iss = _canonical_issuer(token_iss)
    if canon_iss not in _trusted_canonical_issuers():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Token verification failed: untrusted issuer "
                f"(configure AUTH0_DOMAIN or AUTH0_ADDITIONAL_ISSUERS): {token_iss}"
            ),
        )

    jwks = await _fetch_jwks_for_canonical_issuer(canon_iss, force=False)
    key_data = _find_key(jwks, kid)

    if key_data is None:
        jwks = await _fetch_jwks_for_canonical_issuer(canon_iss, force=True)
        key_data = _find_key(jwks, kid)

    if key_data is None:
        available = [k.get("kid") for k in jwks.get("keys", [])]
        token_aud = unverified_payload.get("aud", "?")
        print(
            f"[Auth0] kid={kid} not in JWKS for iss={token_iss}. "
            f"Available: {available}. aud={token_aud} alg={alg}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Token verification failed: no signing key for kid={kid} "
                f"(iss={token_iss}, aud={token_aud})"
            ),
        )

    return RSAAlgorithm.from_jwk(key_data), token_iss


# ---------------------------------------------------------------------------
# verify_token dependency
# ---------------------------------------------------------------------------


async def verify_token(
    credentials=Depends(security),
) -> dict:
    """
    Verify an Auth0 JWT via RS256 JWKS signature verification.

    Uses the token issuer to fetch the correct JWKS. Validates audience against
    the native client id and API audience (and optional extras).

    Returns a dict with at least {"sub": "<auth0-user-id>", ...}.

    Raises:
        HTTPException 401: if the token is invalid, expired, or unverifiable.
    """
    token = credentials.credentials

    cached = _cache_get(token)
    if cached:
        return cached

    print(f"[Auth0] Verifying token (first 40 chars): {token[:40]}...")
    signing_key, token_iss = await _get_signing_key_and_issuer(token)
    audiences = _allowed_audiences()
    decode_audience: str | list[str] = audiences[0] if len(audiences) == 1 else audiences

    try:
        payload: dict = pyjwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=decode_audience,
            issuer=token_iss,
        )
    except ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        ) from e
    except InvalidTokenError as e:
        msg = str(e)
        hint = ""
        if "audience" in msg.lower():
            hint = (
                " Ensure AUTH0_CLIENT_ID matches your mobile/web Auth0 Application "
                "client ID (id_token `aud`), or add values via AUTH0_ADDITIONAL_AUDIENCES."
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}{hint}",
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
