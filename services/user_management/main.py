# uvicorn services.user_management.main:app --host 0.0.0.0 --port 8000 --reload

import os
import sys
import time
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

# Add current directory to path to import libs and models
# In Docker, main.py is at /app/, and libs/ and models/ are also at /app/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# for postgresql
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# for postgresql
from libs.db import get_db
from models.user_models import User

app = FastAPI(
    title="SafeRoute API (Mock)",
    description=(
        "Mock implementation of SafeRoute backend APIs based on the architecture spec. "
        "Endpoints return example / in-memory stub data for interactive testing via /docs."
    ),
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= Auth Token Configuration =========
# Auth token expiration time in seconds (default: 1 hour)
AUTH_TOKEN_TTL = int(os.getenv("AUTH_TOKEN_TTL", "3600"))

# ========= In-memory mock storage =========
users: Dict[str, dict] = {}
trusted_contacts: Dict[str, List[dict]] = {}
notifications: Dict[str, dict] = {}
routes: Dict[str, dict] = {}
nav_sessions: Dict[str, dict] = {}
feedback_store: Dict[str, dict] = {}
audit_logs: List[dict] = []
data_batches: Dict[str, dict] = {}
emergency_status: Dict[str, dict] = {}

# ========= Shared Models =========
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
    lat: float
    lon: float


# ========= User Management Models =========


class RegisterRequest(BaseModel):
    email: str
    password_hash: str
    device_id: str
    phone: Optional[str] = None
    name: Optional[str] = None


class AuthInfo(BaseModel):
    token: str
    expires_in: int = 3600


class RegisterResponse(BaseModel):
    user_id: str
    status: Literal["created"]
    auth: AuthInfo
    email: str
    phone: Optional[str] = None
    name: Optional[str] = None
    device_id: str
    created_at: datetime


class LoginRequest(BaseModel):
    email: str
    password_hash: str
    device_id: str


class LoginResponse(BaseModel):
    user_id: str
    status: Literal["authenticated"]
    auth: AuthInfo
    email: str
    device_id: str
    last_login: datetime


class PreferencesRequest(BaseModel):
    voice_guidance: Literal["on", "off"]
    safety_bias: Optional[Literal["safest", "fastest"]] = None
    units: Optional[Literal["metric", "imperial"]] = None


class PreferencesResponse(BaseModel):
    user_id: str
    status: Literal["preferences_saved"]
    preferences: PreferencesRequest
    updated_at: datetime


class TrustedContactUpsertRequest(BaseModel):
    contact_id: Optional[str] = None
    name: str
    phone: str
    relationship: Optional[str] = None
    is_primary: Optional[bool] = None


class TrustedContact(BaseModel):
    contact_id: str
    name: str
    phone: str
    relationship: Optional[str] = None
    is_primary: Optional[bool] = None


class TrustedContactUpsertResponse(BaseModel):
    user_id: str
    contact_id: str
    status: Literal["contact_upserted"]
    contact: TrustedContact
    updated_at: datetime


class UserResponse(BaseModel):
    user_id: str
    name: Optional[str]
    email: str
    phone: Optional[str] = None
    created_at: datetime
    last_login: Optional[datetime] = None


class TrustedContactsListResponse(BaseModel):
    user_id: str
    contacts: List[TrustedContact]


# ========= User Management =========


@app.get("/")
async def root():
    return {"service": "user_management", "status": "running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "user_management"}


@app.get("/debug/db-info")
async def debug_db_info():
    """Debug endpoint to show database connection info (without password)."""
    import os
    from urllib.parse import urlparse

    db_url = os.getenv("DATABASE_URL")
    if db_url:
        # Parse and redact password
        parsed = urlparse(db_url)
        safe_url = f"{parsed.scheme}://{parsed.username}:***@{parsed.hostname}:{parsed.port}{parsed.path}"
    else:
        db_host = os.getenv("DATABASE_HOST", "127.0.0.1")
        db_port = os.getenv("DATABASE_PORT", "5432")
        db_user = os.getenv("DATABASE_USER", "saferoute")
        db_name = os.getenv("DATABASE_NAME", "saferoute")
        safe_url = f"postgresql+asyncpg://{db_user}:***@{db_host}:{db_port}/{db_name}"

    return {
        "database_url": safe_url,
        "database_host": os.getenv("DATABASE_HOST", "127.0.0.1"),
        "database_port": os.getenv("DATABASE_PORT", "5432"),
        "database_name": os.getenv("DATABASE_NAME", "saferoute"),
        "database_user": os.getenv("DATABASE_USER", "saferoute"),
    }


#
@app.post(
    "/v1/users/register", response_model=RegisterResponse, tags=["User Management"]
)
##add db session dependency
async def register_user(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # 1) 先检查 email 是否已存在（防止重复注册）
    result = await db.execute(select(User).where(User.email == payload.email))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # 2) 写入 PostgreSQL
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

    # 3) Store in memory
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


@app.post("/v1/auth/login", response_model=LoginResponse, tags=["User Management"])
@app.post("/v1/auth/login", response_model=LoginResponse, tags=["User Management"])
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # 先从 PostgreSQL 查用户
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if not user or user.password_hash != payload.password_hash:
        # 不区分“用户不存在”和“密码错误”，防止枚举
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # 2) 更新 last_login
    user.last_login = now
    db.add(user)  # Ensure the session tracks the change
    await db.commit()

    # 3) Update in-memory storage
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
async def save_preferences(user_id: str, payload: PreferencesRequest):
    now = datetime.utcnow()

    # Get or create user data
    users.setdefault(user_id, {"user_id": user_id})
    user_data = users[user_id]

    # Update preferences
    user_data["preferences"] = payload.dict()
    user_data["updated_at"] = now

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
async def upsert_trusted_contact(user_id: str, payload: TrustedContactUpsertRequest):
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
    now = datetime.utcnow()
    stored = [c for c in contacts if c["contact_id"] == contact_id][0]
    contact_obj = TrustedContact(**stored)
    return TrustedContactUpsertResponse(
        user_id=user_id,
        contact_id=contact_id,
        status="contact_upserted",
        contact=contact_obj,
        updated_at=now,
    )


@app.get("/v1/users/{user_id}", response_model=UserResponse, tags=["User Management"])
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    # 1) Query PostgreSQL database (primary source of truth)
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

    # 2) Fallback to in-memory storage (legacy compatibility)
    u = users.get(user_id)
    if u:
        # Remove password_hash from response
        u = u.copy()
        u.pop("password_hash", None)
        return UserResponse(**u)

    # 3) User not found - return 404
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
    include_inactive: bool = Query(False, description="Mock flag; no effect in stub"),
):
    contacts = trusted_contacts.get(user_id, [])
    return TrustedContactsListResponse(
        user_id=user_id,
        contacts=[TrustedContact(**c) for c in contacts],
    )


@app.get("/auth0/callback", tags=["Auth"])
@app.post("/auth0/callback", tags=["Auth"])
async def auth0_callback(code: Optional[str] = None, state: Optional[str] = None):
    """
    Placeholder Auth0 OAuth2 callback endpoint.

    Currently just echoes code/state so the URL can be configured in Auth0.
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
