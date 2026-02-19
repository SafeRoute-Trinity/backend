# Run:
# uvicorn services.user_management.main:app --host 0.0.0.0 --port 20000 --reload
# Docs: http://127.0.0.1:20000/docs

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

from fastapi import Depends, HTTPException, Query, Request, Response, status
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from models.audit import Audit

# Add parent directory to path to import libs and models
# In Docker, main.py is at /app/, and libs/ and models/ are also at /app/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from common.constants import AUTH_TOKEN_TTL
from libs.auth.auth0_verify import verify_token
from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from models.user_models import TrustedContact, User, UserPreferences

# Initialize database connections
initialize_databases([DatabaseType.POSTGRES])

# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)

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


# ======== Audit Log Models ===========================
class AuditLogResponse(BaseModel):
    log_id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    event_type: str
    event_id: Optional[uuid.UUID] = None
    message: str
    created_at: datetime
    updated_at: datetime


class AuditListResponse(BaseModel):
    data: List[AuditLogResponse]
    total: int
    page: int
    page_size: int


# ========= User Management Models =========


class RegisterRequest(BaseModel):
    """Request model for user registration."""

    email: str
    password: str
    phone: Optional[str] = None
    name: Optional[str] = None


class AuthInfo(BaseModel):
    """Authentication token information."""

    token: str
    expires_in: int = 3600


class RegisterResponse(BaseModel):
    """Response model for user registration."""

    user_id: uuid.UUID
    status: Literal["created"]
    auth: AuthInfo
    email: str
    phone: Optional[str] = None
    name: Optional[str] = None
    created_at: datetime


class LoginRequest(BaseModel):
    """Request model for user login."""

    email: str
    password: str


class LoginResponse(BaseModel):
    """Response model for user login."""

    user_id: uuid.UUID
    status: Literal["authenticated"]
    auth: AuthInfo
    email: str
    last_login: datetime


class UserResponse(BaseModel):
    """Response model for user information."""

    user_id: uuid.UUID
    name: Optional[str]
    email: str
    phone: Optional[str] = None
    created_at: datetime
    last_login: Optional[datetime] = None


class PreferencesRequest(BaseModel):
    """Request model for user preferences."""

    voice_guidance: bool
    safety_bias: Optional[Literal["safest", "fastest"]] = None
    units: Optional[Literal["metric", "imperial"]] = None


class PreferencesResponse(BaseModel):
    """Response model for saved/loaded preferences."""

    user_id: uuid.UUID
    status: Literal["preferences_saved", "preferences_loaded"]
    preferences: PreferencesRequest
    updated_at: datetime


class TrustedContactUpsertRequest(BaseModel):
    """Request model for creating/updating trusted contacts."""

    name: str
    phone: str
    relationship: Optional[str] = None
    is_primary: Optional[bool] = None


class TrustedContactDTO(BaseModel):
    """Pure contact item for API responses (NOT ORM)."""

    model_config = ConfigDict(from_attributes=True)

    contact_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    phone: str
    relationship: Optional[str] = Field(default=None, alias="relation")
    is_primary: bool
    created_at: datetime
    updated_at: datetime


class TrustedContactUpsertResponse(BaseModel):
    user_id: uuid.UUID
    status: Literal["contact_upserted"]
    contact: TrustedContactDTO
    updated_at: datetime


class TrustedContactsListResponse(BaseModel):
    user_id: uuid.UUID
    contacts: List[TrustedContactDTO]


# ========= User Management Endpoints =========


@app.get("/")
async def root():
    """
    Root endpoint for service health check.

    Returns:
        Dict with service name and status
    """
    return {"service": "user_management", "status": "running"}


@app.get(
    "/v1/users/{user_id}",
    response_model=UserResponse,
    tags=["User Management"],
)
async def get_user(
    user_id: str,
    # auth: dict = Depends(verify_token),
    db=Depends(get_db),
):
    """
    Get user information by user ID (DB is the source of truth).

    Raises:
        HTTPException: 400 if user_id format invalid
        HTTPException: 404 if user not found
    """
    # Validate UUID
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    # Query PostgreSQL database
    result = await db.execute(select(User).where(User.user_id == user_uuid))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )

    # Build response payload (only fields that exist in DB)
    payload = {
        "user_id": user.user_id,
        "email": user.email,
        "name": user.name,
        "phone": user.phone,
        "created_at": user.created_at,
        "updated_at": getattr(user, "updated_at", None),
        "last_login": user.last_login,
    }

    # If your UserResponse doesn't include updated_at, remove it:
    # payload.pop("updated_at", None)

    return UserResponse(**payload)


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
    DB schema (saferoute.users):
      - user_id UUID PK default gen_random_uuid()
      - created_at timestamptz not null default now()
      - updated_at timestamptz not null default now()
      - last_login timestamptz null
      - email unique
    """
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

    user_id = uuid.uuid4()
    # Create user row.
    # Let DB generate: user_id, created_at, updated_at (per DB defaults)
    user = User(
        user_id=user_id,
        email=payload.email,
        password=payload.password,  # TODO: hash this
        phone=payload.phone,
        name=payload.name,
        last_login=None,
    )

    db.add(user)

    # Flush so DB defaults (user_id, created_at, updated_at) are available on `user`
    await db.flush()

    # Now we can safely use DB-generated UUID

    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=user_id,
        event_type="authentication",
        event_id=user_id,
        message="Register",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        # Most common: unique violation on email
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create user: {e}",
        )

    # Backward compatibility: in-memory store
    # Use DB values for created_at/updated_at if your ORM model includes them;
    # fall back to `now` otherwise.
    created_at_val = getattr(user, "created_at", now)
    users[user_id] = {
        "user_id": user_id,
        "email": user.email,
        "phone": user.phone,
        "name": user.name,
        "password": user.password,
        "created_at": created_at_val,
        "last_login": None,
    }

    # Business metric: increment registrations counter (only once)
    USER_REGISTRATION_TOTAL.inc()

    return RegisterResponse(
        user_id=user_id,
        status="created",
        auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
        email=user.email,
        phone=user.phone,
        name=user.name,
        created_at=created_at_val,
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
    DB schema (saferoute.users):
      - last_login timestamptz NULL
      - updated_at timestamptz NOT NULL default now()
    """
    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # Query PostgreSQL database for user
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or user.password != payload.password:
        # Don't distinguish between "user not found" and "wrong password"
        # to prevent user enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Update timestamps
    user.last_login = now
    # If your User ORM includes updated_at (it should, per DB schema), update it too
    if hasattr(user, "updated_at"):
        user.updated_at = now

    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=user.user_id,
        event_type="authentication",
        event_id=user.user_id,
        message="Login",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not login",
        )

    # Update in-memory cache (optional, can be removed later)
    users[user.user_id] = {
        "user_id": user.user_id,
        "email": user.email,
        "phone": user.phone,
        "name": user.name,
        "password": user.password,
        "created_at": user.created_at,
        "last_login": now,
    }

    return LoginResponse(
        user_id=user.user_id,
        status="authenticated",
        auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
        email=user.email,
        last_login=now,
    )


@app.get(
    "/v1/users/{user_id}/preferences",
    response_model=PreferencesResponse,
    tags=["User Management"],
)
async def get_preferences(
    user_id: str,
    db=Depends(get_db),
):
    """
    Get user preferences from PostgreSQL (saferoute.user_preferences).

    Raises:
      - 400 if user_id invalid
      - 404 if user not found or preferences not set
    """
    # ---- (0) Parse user_id from path ----
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    # ---- (1) Ensure user exists (recommended) ----
    result = await db.execute(select(User).where(User.user_id == user_uuid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (2) Fetch preferences ----
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_uuid))
    pref = result.scalar_one_or_none()

    if not pref:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found",
        )

    return PreferencesResponse(
        user_id=user_uuid,
        status="preferences_loaded",
        preferences=PreferencesRequest(
            user_id=user_uuid,
            voice_guidance=pref.voice_guidance,
            units=pref.units,
        ),
        updated_at=pref.updated_at,
    )


@app.post(
    "/v1/users/{user_id}/preferences",
    response_model=PreferencesResponse,
    tags=["User Management"],
)
async def save_preferences(
    user_id: str,
    payload: PreferencesRequest,
    # auth: dict = Depends(verify_token), # Temporarily not use auth
    db=Depends(get_db),
):
    """
    Save user preferences into PostgreSQL (saferoute.user_preferences).

    DB schema:
      - user_id uuid PK/FK
      - voice_guidance boolean not null default true
      - units varchar(20) not null default 'metric' (metric|imperial)
      - created_at/updated_at timestamptz not null default now()
    """
    now = datetime.utcnow()

    # ---- (0) Parse user_id from path ----
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    # Optional but recommended: ensure user exists
    result = await db.execute(select(User).where(User.user_id == user_uuid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (1) Upsert into user_preferences ----
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_uuid))
    pref = result.scalar_one_or_none()

    if pref:
        pref.voice_guidance = payload.voice_guidance
        pref.units = payload.units
        pref.updated_at = now
    else:
        pref = UserPreferences(
            user_id=user_uuid,
            voice_guidance=payload.voice_guidance,
            units=payload.units,
            # created_at has DB default now(); can omit, but ok to set explicitly if you want:
            # created_at=now,
            updated_at=now,
        )
        db.add(pref)
        await db.flush()

    # ---- (2) Audit ----
    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=user_uuid,
        event_type="authentication",
        event_id=user_uuid,
        message="save_preference",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    # ---- (3) Commit ----
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not update preference",
        )

    return PreferencesResponse(
        user_id=user_uuid,
        status="preferences_saved",
        preferences=PreferencesRequest(
            user_id=user_uuid,
            voice_guidance=pref.voice_guidance,
            units=pref.units,
        ),
        updated_at=now,
    )


@app.get(
    "/v1/users/{user_id}/trusted-contacts",
    response_model=TrustedContactsListResponse,
    tags=["User Management"],
)
async def list_trusted_contacts(
    user_id: str,
    db=Depends(get_db),
):
    """
    List all trusted contacts for a user from PostgreSQL (saferoute.contacts).
    """

    # ---- (1) Validate UUID ----
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    # ---- (2) Ensure user exists ----
    user = await db.scalar(select(User).where(User.user_id == user_uuid))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (3) Query contacts ----
    stmt = (
        select(TrustedContact)
        .where(TrustedContact.user_id == user_uuid)
        .order_by(
            desc(TrustedContact.is_primary),
            TrustedContact.created_at.asc(),
        )
    )

    contacts = (await db.scalars(stmt)).all()

    # ---- (4) ORM → DTO (不要直接回 ORM!) ----
    items = [TrustedContactDTO.model_validate(c) for c in contacts]

    return TrustedContactsListResponse(
        user_id=user_uuid,
        contacts=items,
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


@app.post(
    "/v1/users/{user_id}/trusted-contacts",
    response_model=TrustedContactUpsertResponse,
    tags=["User Management"],
)
async def upsert_trusted_contact(
    user_id: str,
    body: TrustedContactUpsertRequest,
    # auth: dict = Depends(verify_token), # Temporarily not use auth
    db=Depends(get_db),
):
    """
    If a contact with same (user_id + phone) exists -> update.
    Otherwise -> create new contact with new UUID.
    """
    now = datetime.utcnow()

    # ---- (1) Validate user_id ----
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    # ---- (2) Ensure user exists ----
    user = await db.scalar(select(User).where(User.user_id == user_uuid))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (3) Try find existing contact (by phone) ----
    contact = await db.scalar(
        select(TrustedContact).where(
            TrustedContact.user_id == user_uuid,
            TrustedContact.phone == body.phone,
        )
    )

    # ---- (4) Update if exists ----
    if contact:
        contact.name = body.name
        contact.relation = body.relationship
        if body.is_primary is not None:
            contact.is_primary = body.is_primary
        contact.updated_at = now

    # ---- (5) Otherwise create new contact ----
    else:
        contact = TrustedContact(
            contact_id=uuid.uuid4(),  # ← 明確產生新 UUID
            user_id=user_uuid,
            name=body.name,
            phone=body.phone,
            relation=body.relationship,
            is_primary=body.is_primary if body.is_primary is not None else False,
            created_at=now,
            updated_at=now,
        )
        db.add(contact)
        await db.flush()

    # ---- (6) Audit ----
    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=user_uuid,
        event_type="authentication",
        event_id=user_uuid,
        message="upsert trusted contact",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    # ---- (7) Commit ----
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not upsert trusted contact",
        )

    # ---- (8) Response ----
    return TrustedContactUpsertResponse(
        user_id=user_uuid,
        status="contact_upserted",
        contact=TrustedContactDTO.model_validate(contact),
        updated_at=now,
    )


@app.get(
    "/v1/audit",
    response_model=AuditListResponse,
    tags=["Audit"],
    summary="List audit logs (paginated)",
)
@app.get(
    "/v1/users/audit",
    response_model=AuditListResponse,
    tags=["Audit"],
    summary="List audit logs (paginated)",
)
async def list_audit_logs(
    user_id: Optional[uuid.UUID] = Query(None),
    event_type: Optional[str] = Query(None),
    start: Optional[datetime] = Query(None, description="Filter created_at >= start"),
    end: Optional[datetime] = Query(None, description="Filter created_at <= end"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db=Depends(get_db),
):
    # 1) build filters
    filters = []
    if user_id is not None:
        filters.append(Audit.user_id == user_id)
    if event_type is not None and event_type != "":
        filters.append(Audit.event_type == event_type)
    if start is not None:
        filters.append(Audit.created_at >= start)
    if end is not None:
        filters.append(Audit.created_at <= end)

    # 2) total count
    count_stmt = select(func.count()).select_from(Audit)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = (await db.execute(count_stmt)).scalar_one()

    # 3) page query
    offset = (page - 1) * page_size
    stmt = select(Audit).order_by(Audit.created_at.desc()).offset(offset).limit(page_size)
    if filters:
        stmt = stmt.where(*filters)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    # 4) map to response
    data = [
        AuditLogResponse(
            log_id=r.log_id,
            user_id=r.user_id,
            event_type=r.event_type,
            event_id=r.event_id,
            message=r.message,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]

    return AuditListResponse(data=data, total=total, page=page, page_size=page_size)


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
