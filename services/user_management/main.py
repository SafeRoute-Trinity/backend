"""
User Management Service for SafeRoute backend.

Provides endpoints for user registration, authentication, preferences,
and trusted contact management.
"""

import os
import sys
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

# Add parent directory to path to import libs and models
# In Docker, main.py is at /app/, and libs/ and models/ are also at /app/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

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

# Auth Token TTL (1 hour in seconds)
AUTH_TOKEN_TTL = 3600

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


@app.get("/health")
async def health():
    """
    Health check endpoint.

    Returns:
        Dict with status and service name
    """
    return {"status": "ok", "service": "user_management"}


@app.post(
    "/v1/users/register",
    response_model=RegisterResponse,
    tags=["User Management"],
    summary="Register a new user",
)
async def register_user(
    payload,
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
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not create user",
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
    payload,
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
async def save_preferences(user_id, payload):
    """
    Save user preferences.

    Args:
        user_id: User identifier
        payload: Preferences to save

    Returns:
        PreferencesResponse with saved preferences and timestamp
    """
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
async def upsert_trusted_contact(user_id, payload):
    """
    Create or update a trusted contact for a user.

    Args:
        user_id: User identifier
        payload: Contact information to create or update

    Returns:
        TrustedContactUpsertResponse with contact details and timestamp
    """
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
async def get_user(user_id, db=Depends(get_db)):
    """
    Get user information by user ID.

    Args:
        user_id: User identifier
        db: Database session dependency

    Returns:
        UserResponse with user details

    Raises:
        HTTPException: 404 if user not found
    """
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
    user_id,
    include_inactive=Query(False, description="Mock flag; no effect in stub"),
):
    """
    List all trusted contacts for a user.

    Args:
        user_id: User identifier
        include_inactive: Flag to include inactive contacts (not implemented)

    Returns:
        TrustedContactsListResponse with list of contacts
    """
    contacts = trusted_contacts.get(user_id, [])
    return TrustedContactsListResponse(
        user_id=user_id,
        contacts=[TrustedContact(**c) for c in contacts],
    )


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
    return {
        "message": "Auth0 callback received",
        "code": code,
        "state": state,
    }
