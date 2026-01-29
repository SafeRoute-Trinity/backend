"""
Auth0 verification module for FastAPI.

Provides JWT token verification using Auth0's JWKS endpoint.
Use verify_token as a FastAPI dependency to protect routes.

Environment variables (with safe defaults for local dev):
    AUTH0_DOMAIN: Auth0 domain (e.g., dev-xxxxxx.us.auth0.com)
    API_AUDIENCE: API audience identifier (e.g., https://api.saferoute.dev)
"""

import json
import os
from typing import Optional

import certifi
import jwt
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPBearer
from jwt.algorithms import RSAAlgorithm

from common.constants import ALGORITHMS, API_AUDIENCE, ISSUER, JWKS_URL

# Security scheme
security = HTTPBearer()


def verify_token(
    credentials=Depends(security),
):
    """
    Verify JWT token issued by Auth0 using JWKS.

    Fetches the JSON Web Key Set (JWKS) from Auth0 and verifies the token
    signature, expiration, audience, and issuer.

    Args:
        credentials: HTTP authorization credentials containing the JWT token

    Returns:
        Dict containing the decoded JWT payload

    Raises:
        HTTPException: If token verification fails for any reason
            - 401: SSL error fetching JWKS
            - 401: HTTP error fetching JWKS
            - 401: Token expired
            - 401: Invalid audience
            - 401: Invalid issuer
            - 401: Token verification failed

    Example:
        ```python
        @app.get("/protected")
        async def protected_route(payload: dict = Depends(verify_token)):
            user_id = payload.get("sub")
            return {"user_id": user_id}
        ```
    """
    token = credentials.credentials
    try:
        # Fetch JWKS with trusted CA bundle
        resp = requests.get(JWKS_URL, timeout=5, verify=certifi.where())
        resp.raise_for_status()
        jwks = resp.json()

        # Match JWK by kid from token header
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise ValueError("Missing 'kid' in token header")
        key_dict = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        if not key_dict:
            raise ValueError("No matching JWK for token 'kid'")

        # Build public key and decode token
        public_key = RSAAlgorithm.from_jwk(json.dumps(key_dict))
        print(f"ℹ️ [Auth0] Attempting to decode token...")
        payload = jwt.decode(
            token,
            public_key,
            algorithms=ALGORITHMS,
            audience=API_AUDIENCE,
            issuer=ISSUER,
        )
        print(f"✅ [Auth0] Token verified successfully for user: {payload.get('sub')}")
        return payload

    except requests.exceptions.SSLError as e:
        print(f"❌ [Auth0] Token verification failed (SSL error fetching JWKS): {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"SSL error fetching JWKS: {e}",
        ) from e
    except requests.RequestException as e:
        print(f"❌ [Auth0] Token verification failed (HTTP error fetching JWKS): {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"HTTP error fetching JWKS: {e}",
        ) from e
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from e
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



# Router for Auth0 verification endpoints
router = APIRouter(prefix="/auth0", tags=["auth"])


@router.get("/verify")
def verify(payload=Depends(verify_token)):
    """
    Protected endpoint that returns user info if token is valid.

    Args:
        payload: Decoded JWT payload from verify_token dependency

    Returns:
        Dict containing validation message and user ID from token
    """
    return {"message": "Token valid ✅", "user": payload.get("sub")}


# Minimal standalone app for local testing
if os.getenv("AUTH0_STANDALONE", "0") == "1":
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
