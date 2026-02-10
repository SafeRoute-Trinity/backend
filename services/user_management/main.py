"""
User Management Service for SafeRoute backend.

Provides endpoints for user registration, authentication, preferences,
and trusted contact management.
"""

import os
import sys
import time
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import Depends, Header, HTTPException, Query, Request, Response, status
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

# Add parent directory to path to import libs and models
# In Docker, main.py is at /app/, and libs/ and models/ are also at /app/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from common.constants import AUTH_TOKEN_TTL
from libs.auth.auth0_verify import verify_token
from libs.db import get_db
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from models.user_models import User

# Create service configuration
service_config = ServiceAppConfig(
    title="SafeRoute API (Mock)",
    description=(
        "Mock implementation of SafeRoute backend APIs based on the "
        "architecture spec. Endpoints return example / in-memory stub data "
        "for interactive testing via /docs."
    ),
    service_name="user_management",
    cors_config=CORSMiddlewareConfig(),
)

# Create factory and build app
factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

# Add business-specific metrics
USER_REGISTRATION_TOTAL = factory.add_business_metric(
    "user_registrations_total",
    "Total user registrations",
)

# In-memory mock storage (for backward compatibility)
users = {}
trusted_contacts = {}
notifications = {}
routes = {}
nav_sessions = {}
feedback_store = {}
audit_logs = []
data_batches = {}
emergency_status = {}


# ========= Helper Functions =========


def extract_user_id_from_auth(auth: dict) -> str:
    """
    Extract user_id from Auth0 authentication token.

    Auth0 'sub' claim format can be:
    - "auth0|user_id" (with provider prefix)
    - "user_id" (without prefix)

    Args:
        auth: Authentication dictionary containing JWT claims

    Returns:
        Extracted user_id string
    """
    auth_sub = auth.get("sub")
    return auth_sub.split("|")[-1] if auth_sub and "|" in auth_sub else auth_sub


# ========= Metrics =========

SERVICE_NAME = "user_management"
registry = CollectorRegistry()

# Generic per-request counter: can be shared across all services
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
    registry=registry,
)

# Request latency histogram per path
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
    registry=registry,
)

# Business metric: total user registrations
USER_REGISTRATION_TOTAL = Counter(
    "user_registrations_total",
    "Total user registrations",
    registry=registry,
)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """
    Middleware to measure:
    - request count
    - latency per path
    for every HTTP request handled by this service.
    """
    start = time.time()
    response = await call_next(request)

    path = request.url.path

    # Increment per-request counter
    REQUEST_COUNT.labels(
        service=SERVICE_NAME,
        method=request.method,
        path=path,
        http_status=response.status_code,
    ).inc()

    # Record latency
    REQUEST_LATENCY.labels(
        service=SERVICE_NAME,
        path=path,
    ).observe(time.time() - start)

    return response


# ========= Shared Models =========


class Point(BaseModel):
    """Geographic point with latitude and longitude."""

    lat: float
    lon: float


# ========= User Management Models =========


class RegisterRequest(BaseModel):
    """Request model for user registration."""

    email: str
    password_hash: str
    device_id: str
    phone: Optional[str] = None
    name: Optional[str] = None


class AuthInfo(BaseModel):
    """Authentication token information."""

    token: str
    expires_in: int = 3600


class RegisterResponse(BaseModel):
    """Response model for user registration."""

    user_id: str
    status: Literal["created"]
    auth: AuthInfo
    email: str
    phone: Optional[str] = None
    name: Optional[str] = None
    device_id: str
    created_at: datetime


class LoginRequest(BaseModel):
    """Request model for user login."""

    email: str
    password_hash: str
    device_id: str


class LoginResponse(BaseModel):
    """Response model for user login."""

    user_id: str
    status: Literal["authenticated"]
    auth: AuthInfo
    email: str
    device_id: str
    last_login: datetime


class PreferencesRequest(BaseModel):
    """Request model for user preferences."""

    voice_guidance: Literal["on", "off"]
    safety_bias: Optional[Literal["safest", "fastest"]] = None
    units: Optional[Literal["metric", "imperial"]] = None


class PreferencesResponse(BaseModel):
    """Response model for saved preferences."""

    user_id: str
    status: Literal["preferences_saved"]
    preferences: PreferencesRequest
    updated_at: datetime


class TrustedContactUpsertRequest(BaseModel):
    """Request model for creating/updating trusted contacts."""

    contact_id: Optional[str] = None
    name: str
    phone: str
    relationship: Optional[str] = None
    is_primary: Optional[bool] = None


class TrustedContact(BaseModel):
    """Model representing a trusted contact."""

    contact_id: str
    name: str
    phone: str
    relationship: Optional[str] = None
    is_primary: Optional[bool] = None


class TrustedContactUpsertResponse(BaseModel):
    """Response model for trusted contact upsert operation."""

    user_id: str
    contact_id: str
    status: Literal["contact_upserted"]
    contact: TrustedContact
    updated_at: datetime


class UserResponse(BaseModel):
    """Response model for user information."""

    user_id: str
    name: Optional[str]
    email: str
    phone: Optional[str] = None
    created_at: datetime
    last_login: Optional[datetime] = None


class TrustedContactsListResponse(BaseModel):
    """Response model for listing trusted contacts."""

    user_id: str
    contacts: List[TrustedContact]


# ========= User Management Endpoints =========


@app.get("/")
async def root():
    """
    Root endpoint for service health check.

    Returns:
        Dict with service name and status
    """
    return {"service": "user_management", "status": "running"}


@app.post(
    "/v1/users/register",
    response_model=RegisterResponse,
    tags=["User Management"],
    summary="Register a new user",
)
async def register_user(
    payload: RegisterRequest,
    db=Depends(get_db),
):
    """
    Register a new user account.

    Args:
        payload: Registration request containing user details
        db: Database session dependency

    Returns:
        RegisterResponse with user ID, auth token, and user details

    Raises:
        HTTPException: 400 if email already registered or creation fails
    """
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # Check if email already exists (prevent duplicate registration)
    result = await db.execute(select(User).where(User.email == payload.email))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Write to PostgreSQL database
    user = User(
        user_id=user_id,
        email=payload.email,
        password_hash=payload.password_hash,
        device_id=payload.device_id,
        phone=payload.phone,
        name=payload.name,
        created_at=now,
        last_login=None,
    )

    db.add(user)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        print(f"[UserMgmt] Database integrity error during user registration: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create user: {e}",
        )

    # Store in memory for backward compatibility (can be removed later)
    users[user_id] = {
        "user_id": user_id,
        "email": payload.email,
        "phone": payload.phone,
        "name": payload.name,
        "device_id": payload.device_id,
        "password_hash": payload.password_hash,
        "created_at": now,
        "last_login": None,
    }

    # Business metric: increment registrations counter
    USER_REGISTRATION_TOTAL.inc()

    # Business metric: bump registrations counter
    USER_REGISTRATION_TOTAL.inc()

    return RegisterResponse(
        user_id=user_id,
        status="created",
        auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
        email=payload.email,
        phone=payload.phone,
        name=payload.name,
        device_id=payload.device_id,
        created_at=now,
    )


@app.post(
    "/v1/auth/login",
    response_model=LoginResponse,
    tags=["User Management"],
)
async def login(
    payload: LoginRequest,
    db=Depends(get_db),
):
    """
    Authenticate a user and return auth token.

    Args:
        payload: Login request with email and password hash
        db: Database session dependency

    Returns:
        LoginResponse with user ID, auth token, and user details

    Raises:
        HTTPException: 401 if email or password is invalid
    """
    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # Query PostgreSQL database for user
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or user.password_hash != payload.password_hash:
        # Don't distinguish between "user not found" and "wrong password"
        # to prevent user enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Update last_login timestamp
    user.last_login = now
    await db.commit()

    # Update in-memory cache (optional, can be removed later)
    users[user.user_id] = {
        "user_id": user.user_id,
        "email": user.email,
        "phone": user.phone,
        "name": user.name,
        "device_id": user.device_id,
        "password_hash": user.password_hash,
        "created_at": user.created_at,
        "last_login": now,
    }

    return LoginResponse(
        user_id=user.user_id,
        status="authenticated",
        auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
        email=user.email,
        device_id=payload.device_id,
        last_login=now,
    )


@app.post(
    "/v1/users/{user_id}/preferences",
    response_model=PreferencesResponse,
    tags=["User Management"],
)
async def save_preferences(
    user_id: str,
    payload: PreferencesRequest,
    auth: dict = Depends(verify_token),
):
    """
    Save user preferences (protected endpoint).

    Args:
        user_id: User identifier (must match authenticated user)
        payload: Preferences to save
        auth: Enhanced auth object from verify_token dependency

    Returns:
        PreferencesResponse with saved preferences and timestamp

    Raises:
        HTTPException: 401 if not authenticated or user_id mismatch
        HTTPException: 403 if user tries to modify another user's preferences
    """
    # Extract user ID from auth sub (format: "auth0|user_id" or just "user_id")
    auth_user_id = extract_user_id_from_auth(auth)

    # Verify user can only modify their own preferences
    if auth_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own preferences",
        )
    now = datetime.utcnow()

    # Get user data from memory
    users.setdefault(user_id, {"user_id": user_id})
    user_data = users[user_id]

    # Update preferences
    user_data["preferences"] = payload.dict()
    user_data["updated_at"] = now.isoformat()

    # Update in memory
    users[user_id] = user_data.copy()
    if isinstance(user_data.get("created_at"), str):
        users[user_id]["created_at"] = datetime.fromisoformat(user_data["created_at"])
    if isinstance(user_data.get("last_login"), str):
        users[user_id]["last_login"] = (
            datetime.fromisoformat(user_data["last_login"]) if user_data.get("last_login") else None
        )

    return PreferencesResponse(
        user_id=user_id,
        status="preferences_saved",
        preferences=payload,
        updated_at=now,
    )


@app.post(
    "/v1/users/{user_id}/trusted-contacts",
    response_model=TrustedContactUpsertResponse,
    tags=["User Management"],
)
async def upsert_trusted_contact(
    user_id: str,
    payload: TrustedContactUpsertRequest,
    auth: dict = Depends(verify_token),
):
    """
    Create or update a trusted contact for a user (protected endpoint).

    Args:
        user_id: User identifier (must match authenticated user)
        payload: Contact information to create or update
        auth: Enhanced auth object from verify_token dependency

    Returns:
        TrustedContactUpsertResponse with contact details and timestamp

    Raises:
        HTTPException: 401 if not authenticated
        HTTPException: 403 if user tries to modify another user's contacts
    """
    # Extract user ID from auth sub (format: "auth0|user_id" or just "user_id")
    auth_user_id = extract_user_id_from_auth(auth)

    # Verify user can only modify their own contacts
    if auth_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only modify your own trusted contacts",
        )
    contacts = trusted_contacts.setdefault(user_id, [])
    if payload.contact_id:
        for c in contacts:
            if c["contact_id"] == payload.contact_id:
                c.update(payload.dict(exclude_unset=True))
                contact_id = payload.contact_id
                break
        else:
            contact_id = payload.contact_id
            contacts.append({**payload.dict(), "contact_id": contact_id})
    else:
        contact_id = f"ctc_{uuid.uuid4().hex[:6]}"
        contacts.append({**payload.dict(), "contact_id": contact_id})
    now: datetime = datetime.utcnow()
    stored = [c for c in contacts if c["contact_id"] == contact_id][0]
    contact_obj = TrustedContact(**stored)
    return TrustedContactUpsertResponse(
        user_id=user_id,
        contact_id=contact_id,
        status="contact_upserted",
        contact=contact_obj,
        updated_at=now,
    )


@app.get(
    "/v1/users/{user_id}",
    response_model=UserResponse,
    tags=["User Management"],
)
async def get_user(
    user_id: str,
    auth: dict = Depends(verify_token),
    db=Depends(get_db),
):
    """
    Get user information by user ID (protected endpoint).

    Args:
        user_id: User identifier (must match authenticated user)
        auth: Enhanced auth object from verify_token dependency
        db: Database session dependency

    Returns:
        UserResponse with user details

    Raises:
        HTTPException: 401 if not authenticated
        HTTPException: 403 if user tries to access another user's data
        HTTPException: 404 if user not found
    """
    # Extract user ID from auth sub (format: "auth0|user_id" or just "user_id")
    auth_user_id = extract_user_id_from_auth(auth)

    # Verify user can only access their own data
    if auth_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access your own user information",
        )
    # Query PostgreSQL database (primary source of truth)
    result = await db.execute(select(User).where(User.user_id == user_id))
    db_user = result.scalar_one_or_none()

    if db_user:
        # Convert database user to dict format
        u = {
            "user_id": db_user.user_id,
            "email": db_user.email,
            "name": db_user.name,
            "phone": db_user.phone,
            "device_id": db_user.device_id,
            "created_at": db_user.created_at,
            "last_login": db_user.last_login,
        }

        # Also update in-memory cache (for backward compatibility)
        users[user_id] = u.copy()

        return UserResponse(**u)

    # Fallback to in-memory storage (legacy compatibility)
    u = users.get(user_id)
    if u:
        # Remove password_hash from response
        u = u.copy()
        u.pop("password_hash", None)
        return UserResponse(**u)

    # User not found - return 404
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"User {user_id} not found",
    )


@app.get(
    "/v1/users/{user_id}/trusted-contacts",
    response_model=TrustedContactsListResponse,
    tags=["User Management"],
)
async def list_trusted_contacts(
    user_id: str,
    auth: dict = Depends(verify_token),
    include_inactive=Query(False, description="Mock flag; no effect in stub"),
):
    """
    List all trusted contacts for a user (protected endpoint).

    Args:
        user_id: User identifier (must match authenticated user)
        auth: Enhanced auth object from verify_token dependency
        include_inactive: Flag to include inactive contacts (not implemented)

    Returns:
        TrustedContactsListResponse with list of contacts

    Raises:
        HTTPException: 401 if not authenticated
        HTTPException: 403 if user tries to access another user's contacts
    """
    # Extract user ID from auth sub (format: "auth0|user_id" or just "user_id")
    auth_user_id = extract_user_id_from_auth(auth)

    # Verify user can only access their own contacts
    if auth_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access your own trusted contacts",
        )
    contacts = trusted_contacts.get(user_id, [])
    return TrustedContactsListResponse(
        user_id=user_id,
        contacts=[TrustedContact(**c) for c in contacts],
    )


@app.get("/login", tags=["Auth"])
async def login_info(iss: Optional[str] = None):
    """
    Auth0 login information endpoint.

    This endpoint is called by Auth0 for validation purposes.
    Returns information about the authentication configuration.
    """
    return {
        "message": "Authentication is handled by Auth0",
        "auth0_domain": "saferoute.eu.auth0.com",
        "issuer": iss,
        "mobile_callback": "saferouteapp://auth/callback",
        "note": "Mobile clients should use Auth0 native authentication",
    }


@app.get("/auth0/callback", tags=["Auth"])
@app.post("/auth0/callback", tags=["Auth"])
async def auth0_callback(code=None, state=None):
    """
    Placeholder Auth0 OAuth2 callback endpoint.

    Currently just echoes code/state so the URL can be configured in Auth0.

    Args:
        code: OAuth authorization code
        state: OAuth state parameter

    Returns:
        Dict with callback message and received parameters
    """

    return {"message": "Auth0 callback received", "code": code, "state": state}

    # ========= Metrics endpoint for Prometheus =========


@app.get("/metrics")
async def metrics_endpoint():
    """
    Expose Prometheus metrics for this service.
    Prometheus will scrape this endpoint inside the cluster.
    """
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


@app.get(
    "/v1/users/me",
    response_model=UserResponse,
    tags=["User Management"],
    summary="Get current user information (protected)",
)
async def get_current_user(
    auth: dict = Depends(verify_token),
    db=Depends(get_db),
):
    """
    Get current user information (protected endpoint example).

    This endpoint demonstrates how to use verify_token for stateless auth:
    - Requires valid JWT token
    - Validates token against Auth0 JWKS

    Mobile app must send:
    - Authorization: Bearer <access_token>

    Args:
        auth: Enhanced auth object from verify_token dependency
            Contains JWT claims
        db: Database session dependency

    Returns:
        UserResponse with user information

    Raises:
        HTTPException: 401 if JWT is invalid
        HTTPException: 404 if user not found in database
    """
    # Extract user_id from sub (format: "auth0|user_id" or just "user_id")
    user_id = extract_user_id_from_auth(auth)

    print(f"[UserMgmt] get_current_user called for: {user_id}")

    # Query PostgreSQL database for user
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in database",
        )

    return UserResponse(
        user_id=user.user_id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        created_at=user.created_at,
        last_login=user.last_login,
    )
