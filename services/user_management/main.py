# Run:
# uvicorn services.user_management.main:app --host 0.0.0.0 --port 20000 --reload
# Docs: http://127.0.0.1:20000/docs

"""
User Management Service for SafeRoute backend.

Provides endpoints for user registration, authentication, preferences,
and trusted contact management.
"""

import httpx
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

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

    Strips the provider prefix (e.g. "auth0|") so the DB stores
    only the raw user ID (e.g. "6979e8045f101df3ab8cff1c").

    Args:
        auth: Authentication dictionary containing JWT claims

    Returns:
        Stripped user_id string for DB storage/lookup
    """
    auth_sub = auth.get("sub", "")
    return auth_sub.split("|", 1)[-1] if "|" in auth_sub else auth_sub


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
    user_id: Optional[str] = None
    event_type: str
    event_id: Optional[uuid.UUID] = None
    message: str
    created_at: datetime
    updated_at: datetime


# ---------- Pagination (shared for list endpoints) ----------


class PaginationMeta(BaseModel):
    """Metadata for paginated list responses."""

    page: int = Field(..., ge=1, description="Current page (1-based)")
    page_size: int = Field(..., ge=1, le=200, description="Items per page")
    total: int = Field(..., ge=0, description="Total number of items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")


def _total_pages(total: int, page_size: int) -> int:
    return max(0, (total + page_size - 1) // page_size) if page_size > 0 else 0


class AuditListResponse(BaseModel):
    """Paginated audit log list with filters and pagination."""

    data: List[AuditLogResponse]
    filters: Dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationMeta


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

    user_id: str
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

    user_id: str
    status: Literal["authenticated"]
    auth: AuthInfo
    email: str
    last_login: datetime


class UserResponse(BaseModel):
    """Response model for user information."""

    user_id: str
    name: Optional[str]
    email: str
    phone: Optional[str] = None
    created_at: datetime
    last_login: Optional[datetime] = None


class UserListResponse(BaseModel):
    """Paginated list of users with filters and pagination."""

    data: List[UserResponse]
    filters: Dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationMeta


class PreferencesRequest(BaseModel):
    """Request model for user preferences."""

    voice_guidance: bool
    safety_bias: Optional[Literal["safest", "fastest"]] = None
    units: Optional[Literal["metric", "imperial"]] = None


class PreferencesResponse(BaseModel):
    """Response model for saved/loaded preferences."""

    user_id: str
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
    user_id: str
    name: str
    phone: str
    relationship: Optional[str] = Field(default=None, alias="relation")
    is_primary: bool
    created_at: datetime
    updated_at: datetime


class TrustedContactUpsertResponse(BaseModel):
    user_id: str
    status: Literal["contact_upserted"]
    contact: TrustedContactDTO
    updated_at: datetime


class TrustedContactsListResponse(BaseModel):
    user_id: str
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
    "/v1/users",
    response_model=UserListResponse,
    tags=["User Management"],
    summary="List users (paginated, filterable)",
)
async def list_users(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    email: Optional[str] = Query(None, description="Filter by email (case-insensitive contains)"),
    name: Optional[str] = Query(None, description="Filter by name (case-insensitive contains)"),
    created_after: Optional[datetime] = Query(
        None, description="Filter users created after this time (inclusive)"
    ),
    created_before: Optional[datetime] = Query(
        None, description="Filter users created before this time (inclusive)"
    ),
    db=Depends(get_db),
):
    """
    List users with pagination and optional filters.

    Useful for admin or support tooling. Filters are combined with AND.
    """
    # Applied filter values for response (convention: empty string when not set)
    filters_resp = {
        "email": email if (email and email.strip()) else "",
        "name": name if (name and name.strip()) else "",
        "created_after": created_after.isoformat() if created_after else "",
        "created_before": created_before.isoformat() if created_before else "",
    }
    # SQL predicates
    predicates = []
    if email is not None and email.strip() != "":
        predicates.append(func.lower(User.email).contains(email.strip().lower()))
    if name is not None and name.strip() != "":
        predicates.append(func.lower(User.name).contains(name.strip().lower()))
    if created_after is not None:
        predicates.append(User.created_at >= created_after)
    if created_before is not None:
        predicates.append(User.created_at <= created_before)

    count_stmt = select(func.count()).select_from(User)
    if predicates:
        count_stmt = count_stmt.where(*predicates)
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    stmt = select(User).order_by(User.created_at.desc()).offset(offset).limit(page_size)
    if predicates:
        stmt = stmt.where(*predicates)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    data = [
        UserResponse(
            user_id=u.user_id,
            name=u.name,
            email=u.email,
            phone=u.phone,
            created_at=u.created_at,
            last_login=u.last_login,
        )
        for u in rows
    ]
    return UserListResponse(
        data=data,
        filters=filters_resp,
        pagination=PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        ),
    )


@app.get(
    "/v1/users/me",
    response_model=UserResponse,
    tags=["User Management"],
    summary="Get current user information (protected)",
)
async def get_current_user(
    request: Request,
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
    # Extract user_id — full sub claim (e.g. "auth0|6979e8...")
    user_id = extract_user_id_from_auth(auth)

    print(f"[UserMgmt] get_current_user called for: {user_id}")

    # Query PostgreSQL database for user
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()

    # Auto-create user on first login — fetch profile from Auth0 /userinfo
    if not user:
        print(f"[UserMgmt] User {user_id} not found, auto-creating from Auth0 /userinfo")

        # Fetch profile from Auth0 /userinfo using the bearer token
        profile = {}
        auth0_domain = os.getenv("AUTH0_DOMAIN", "saferouteapp.eu.auth0.com")
        try:
            token = request.headers.get("Authorization", "").replace("Bearer ", "")
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://{auth0_domain}/userinfo",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    profile = resp.json()
                    print(f"[UserMgmt] Got Auth0 profile: email={profile.get('email')}")
                else:
                    print(f"[UserMgmt] /userinfo returned {resp.status_code}")
        except Exception as e:
            print(f"[UserMgmt] Failed to fetch /userinfo: {e}")

        now = datetime.utcnow()
        user = User(
            user_id=user_id,
            email=profile.get("email") or f"{user_id}@unknown",
            name=profile.get("name") or profile.get("nickname") or None,
            phone=profile.get("phone") or auth.get("https://saferouteapp.eu.auth0.com/phone") or None,
            created_at=now,
            updated_at=now,
            last_login=now,
        )
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
            USER_REGISTRATION_TOTAL.inc()
            print(f"[UserMgmt] Auto-created user {user_id}")
        except Exception as e:
            await db.rollback()
            print(f"[UserMgmt] Failed to auto-create user {user_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create user: {e}",
            )

    # Update last_login
    user.last_login = datetime.utcnow()
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    return UserResponse(
        user_id=user.user_id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        created_at=user.created_at,
        last_login=user.last_login,
    )


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
        HTTPException: 404 if user not found
    """
    # Query PostgreSQL database
    result = await db.execute(select(User).where(User.user_id == user_id))
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

    return UserResponse(**payload)


# --- COMMENTED OUT: Auth0 handles registration/login ---
# These endpoints are no longer needed since Auth0 manages user
# authentication. Users are synced to our DB via the
# POST /v1/webhooks/auth0/sync-user webhook below.
#
# @app.post(
#     "/v1/users/register",
#     response_model=RegisterResponse,
#     tags=["User Management"],
#     summary="Register a new user",
# )
# async def register_user(payload: RegisterRequest, db=Depends(get_db)):
#     ...  # See git history for original implementation
#
# @app.post(
#     "/v1/auth/login",
#     response_model=LoginResponse,
#     tags=["User Management"],
# )
# async def login(payload: LoginRequest, db=Depends(get_db)):
#     ...  # See git history for original implementation
# --- END COMMENTED OUT ---


# ========= Auth0 Webhook =========


class Auth0SyncRequest(BaseModel):
    """Request model for Auth0 post-login user sync."""

    user_id: str  # Auth0 user ID suffix (e.g. "2wst54...")
    email: str
    name: Optional[str] = None
    phone: Optional[str] = None


@app.post("/v1/webhooks/auth0/sync-user", tags=["Auth0 Webhooks"])
async def sync_auth0_user(
    payload: Auth0SyncRequest,
    request: Request,
    db=Depends(get_db),
):
    """
    Webhook called by Auth0 Post-Login Action to upsert user data.

    Auth0 calls this on every login (including first login after sign-up).
    Creates the user if new, updates fields if existing.

    Security: Validates shared secret via X-Auth0-Webhook-Secret header.
    """
    # Verify webhook secret
    secret = request.headers.get("X-Auth0-Webhook-Secret")
    expected_secret = os.getenv("AUTH0_WEBHOOK_SECRET")
    if not expected_secret or secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    # Strip auth0| prefix for DB storage
    raw_user_id = payload.user_id.split("|", 1)[-1] if "|" in payload.user_id else payload.user_id

    # Upsert: create if new, update if exists
    result = await db.execute(select(User).where(User.user_id == raw_user_id))
    user = result.scalar_one_or_none()

    now = datetime.utcnow()
    if user:
        user.email = payload.email
        user.name = payload.name
        user.last_login = now
        user.updated_at = now
    else:
        user = User(
            user_id=raw_user_id,
            email=payload.email,
            name=payload.name,
            phone=payload.phone,
            last_login=now,
        )
        db.add(user)

    # Audit log
    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=raw_user_id,
        event_type="authentication",
        message=f"Auth0 sync ({'update' if user else 'create'})",
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
            detail="Could not sync user",
        )

    # Business metric
    USER_REGISTRATION_TOTAL.inc()

    return {"status": "synced", "user_id": raw_user_id}


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
      - 404 if user not found or preferences not set
    """
    # ---- (1) Ensure user exists ----
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (2) Fetch preferences ----
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
    pref = result.scalar_one_or_none()

    if not pref:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preferences not found",
        )

    return PreferencesResponse(
        user_id=user_id,
        status="preferences_loaded",
        preferences=PreferencesRequest(
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
      - user_id varchar(255) PK/FK
      - voice_guidance boolean not null default true
      - units varchar(20) not null default 'metric' (metric|imperial)
      - created_at/updated_at timestamptz not null default now()
    """
    now = datetime.utcnow()

    # Ensure user exists
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (1) Upsert into user_preferences ----
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
    pref = result.scalar_one_or_none()

    if pref:
        pref.voice_guidance = payload.voice_guidance
        pref.units = payload.units
        pref.updated_at = now
    else:
        pref = UserPreferences(
            user_id=user_id,
            voice_guidance=payload.voice_guidance,
            units=payload.units,
            updated_at=now,
        )
        db.add(pref)
        await db.flush()

    # ---- (2) Audit ----
    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=user_id,
        event_type="authentication",
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
        user_id=user_id,
        status="preferences_saved",
        preferences=PreferencesRequest(
            voice_guidance=pref.voice_guidance,
            units=pref.units,
        ),
        updated_at=now,
    )


@app.get(
    "/v1/users/{user_id}/trusted-contacts",
    response_model=TrustedContactsListResponse,
    tags=["User Management"],
    summary="List trusted contacts (paginated, filterable)",
)
async def list_trusted_contacts(
    user_id: str,
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    is_primary: Optional[bool] = Query(None, description="Filter by primary contact (true/false)"),
    search: Optional[str] = Query(None, description="Filter by name (case-insensitive contains)"),
    db=Depends(get_db),
):
    """
    List trusted contacts for a user from PostgreSQL (saferoute.contacts).
    Supports pagination and optional filters (is_primary, name search).
    """

    # ---- (1) Ensure user exists ----
    user = await db.scalar(select(User).where(User.user_id == user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (2) Query contacts ----
    stmt = (
        select(TrustedContact)
        .where(TrustedContact.user_id == user_id)
        .order_by(
            desc(TrustedContact.is_primary),
            TrustedContact.created_at.asc(),
        )
        .offset(offset)
        .limit(page_size)
    )
    contacts = (await db.scalars(stmt)).all()

    # ---- (3) ORM → DTO ----
    items = [TrustedContactDTO.model_validate(c) for c in contacts]

    return TrustedContactsListResponse(
        user_id=user_id,
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

    # ---- (1) Ensure user exists ----
    user = await db.scalar(select(User).where(User.user_id == user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # ---- (2) Try find existing contact (by phone) ----
    contact = await db.scalar(
        select(TrustedContact).where(
            TrustedContact.user_id == user_id,
            TrustedContact.phone == body.phone,
        )
    )

    # ---- (3) Update if exists ----
    if contact:
        contact.name = body.name
        contact.relation = body.relationship
        if body.is_primary is not None:
            contact.is_primary = body.is_primary
        contact.updated_at = now

    # ---- (4) Otherwise create new contact ----
    else:
        contact = TrustedContact(
            contact_id=uuid.uuid4(),
            user_id=user_id,
            name=body.name,
            phone=body.phone,
            relation=body.relationship,
            is_primary=body.is_primary if body.is_primary is not None else False,
            created_at=now,
            updated_at=now,
        )
        db.add(contact)
        await db.flush()

    # ---- (5) Audit ----
    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=user_id,
        event_type="authentication",
        message="upsert trusted contact",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    # ---- (6) Commit ----
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not upsert trusted contact",
        )

    # ---- (7) Response ----
    return TrustedContactUpsertResponse(
        user_id=user_id,
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
    user_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    start: Optional[datetime] = Query(None, description="Filter created_at >= start"),
    end: Optional[datetime] = Query(None, description="Filter created_at <= end"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db=Depends(get_db),
):
    # 1) Filters for response (convention: empty string when not set)
    filters_resp = {
        "user_id": str(user_id) if user_id is not None else "",
        "event_type": event_type if (event_type and event_type.strip()) else "",
        "start": start.isoformat() if start is not None else "",
        "end": end.isoformat() if end is not None else "",
    }
    # 2) SQL predicates
    predicates = []
    if user_id is not None:
        predicates.append(Audit.user_id == user_id)
    if event_type is not None and event_type != "":
        predicates.append(Audit.event_type == event_type)
    if start is not None:
        predicates.append(Audit.created_at >= start)
    if end is not None:
        predicates.append(Audit.created_at <= end)

    # 3) total count
    count_stmt = select(func.count()).select_from(Audit)
    if predicates:
        count_stmt = count_stmt.where(*predicates)
    total = (await db.execute(count_stmt)).scalar_one()

    # 4) page query
    offset = (page - 1) * page_size
    stmt = select(Audit).order_by(Audit.created_at.desc()).offset(offset).limit(page_size)
    if predicates:
        stmt = stmt.where(*predicates)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    # 5) map to response
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

    return AuditListResponse(
        data=data,
        filters=filters_resp,
        pagination=PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        ),
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

    return {"message": "Auth0 callback received", "code": code, "state": state}

    # ========= Metrics endpoint for Prometheus =========


@app.get("/metrics")
async def metrics_endpoint():
    """
    Expose Prometheus metrics for this service.
    Prometheus will scrape this endpoint inside the cluster.
    """
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

