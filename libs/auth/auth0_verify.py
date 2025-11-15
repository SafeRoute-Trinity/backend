# libs/auth/auth0_verify.py
"""
Auth0 verification module for FastAPI.

- verify_token: FastAPI dependency to protect routes
- router:      /auth0/verify endpoint for quick health check
- (optional) app: minimal FastAPI app for standalone local testing

Env vars (with safe defaults for local dev):
- AUTH0_DOMAIN   e.g. dev-xxxxxx.us.auth0.com
- API_AUDIENCE   e.g. https://api.saferoute.dev
"""

import json
import os

import certifi
import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.algorithms import RSAAlgorithm

# ---------- Config ----------
AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "dev-ne8wedb5815zl4wf.us.auth0.com")
API_AUDIENCE = os.getenv("API_AUDIENCE", "https://api.saferoute.dev")
ISSUER = f"https://{AUTH0_DOMAIN}/"
JWKS_URL = f"{ISSUER}.well-known/jwks.json"
ALGORITHMS = ["RS256"]

# ---------- Security scheme ----------
security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Verify JWT issued by Auth0 using JWKS (fetched via requests/certifi).
    Use as a FastAPI dependency on protected routes.
    """
    token = credentials.credentials
    try:
        # 1) Fetch JWKS with trusted CA bundle
        resp = requests.get(JWKS_URL, timeout=5, verify=certifi.where())
        resp.raise_for_status()
        jwks = resp.json()

        # 2) Match JWK by kid from token header
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise ValueError("Missing 'kid' in token header")
        key_dict = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        if not key_dict:
            raise ValueError("No matching JWK for token 'kid'")

        # 3) Build public key and decode
        public_key = RSAAlgorithm.from_jwk(json.dumps(key_dict))
        payload = jwt.decode(
            token,
            public_key,
            algorithms=ALGORITHMS,
            audience=API_AUDIENCE,
            issuer=ISSUER,
        )
        return payload

    except requests.exceptions.SSLError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"SSL error fetching JWKS: {e}",
        ) from e
    except requests.RequestException as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"HTTP error fetching JWKS: {e}",
        ) from e
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        ) from e
    except jwt.InvalidAudienceError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid audience"
        ) from e
    except jwt.InvalidIssuerError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid issuer"
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {e}",
        ) from e


# ---------- Router (recommended) ----------
router = APIRouter(prefix="/auth0", tags=["auth"])


@router.get("/verify")
def verify(payload: dict = Depends(verify_token)):
    """Protected endpoint—returns user info if token is valid."""
    return {"message": "Token valid ✅", "user": payload.get("sub")}


# ---------- Minimal standalone app (optional for local testing) ----------
if os.getenv("AUTH0_STANDALONE", "0") == "1":
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
