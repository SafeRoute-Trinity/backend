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

from common.auth.session import get_session_manager
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


def verify_token_with_session(
    token_payload: dict = Depends(verify_token),
    session_id: Optional[str] = Header(None, alias="X-Session-Id", description="Server session ID"),
    device_id: Optional[str] = Header(None, alias="X-Device-Id", description="Device ID (optional)"),
) -> dict:
    """
    Enhanced authentication dependency that combines JWT verification + Redis session check.
    
    This is the recommended dependency for protected endpoints in mobile apps.
    
    Validation flow:
    1. Verify JWT (signature/aud/iss/exp) - via verify_token
    2. Check Redis session exists - read session:<sid>
    3. Match session to token subject - ensure session.sub == token.sub
    4. (Optional) Check device_id matches session record
    5. Update last_seen_at for sliding TTL
    
    Mobile app must send:
    - Authorization: Bearer <access_token> (Auth0 JWT)
    - X-Session-Id: <sid> (server session ID from /session/start)
    - X-Device-Id: <device_id> (optional, for additional security)
    
    Args:
        token_payload: Decoded JWT payload from verify_token dependency
        session_id: Session ID from X-Session-Id header
        device_id: Device ID from X-Device-Id header (optional)
        
    Returns:
        Enhanced dict containing:
        - All JWT claims (sub, exp, etc.)
        - session_data: Session information from Redis
        - session_id: Session ID
        
    Raises:
        HTTPException: 401 if JWT is invalid, session not found, or session doesn't match user
        HTTPException: 503 if Redis is unavailable (fail closed)
        
    Example:
        ```python
        @app.get("/protected")
        async def protected_route(auth: dict = Depends(verify_token_with_session)):
            user_id = auth["sub"]
            session_id = auth["session_id"]
            return {"user_id": user_id, "session_id": session_id}
        ```
    """
    # Extract user ID from JWT
    sub = token_payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing 'sub' claim",
        )

    # Session ID is required
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Session-Id header. Please call /session/start first.",
        )

    # Get session manager
    session_manager = get_session_manager()

    # Check Redis availability (fail closed)
    if not session_manager.redis.is_connected():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is unavailable. Session validation cannot proceed. "
            "This is a fail-closed security policy.",
        )

    # Get session from Redis
    session_data = session_manager.get_session(session_id)
    
    if not session_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session not found or expired. Please login again.",
        )

    # Verify session belongs to this user (prevents session stealing)
    if session_data.get("sub") != sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session does not match token user. Session may have been stolen.",
        )

    # Optional: Verify device_id matches (additional security layer)
    if device_id:
        session_device_id = session_data.get("device_id")
        if session_device_id and session_device_id != device_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Device ID mismatch. Session may have been stolen.",
            )

    # Update last_seen_at for sliding TTL (if enabled)
    # This happens in the background and doesn't block the request
    session_manager.update_last_seen(session_id)

    # Return enhanced auth object
    return {
        **token_payload,  # All JWT claims
        "session_id": session_id,
        "session_data": session_data,
    }


# Minimal standalone app for local testing
if os.getenv("AUTH0_STANDALONE", "0") == "1":
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
