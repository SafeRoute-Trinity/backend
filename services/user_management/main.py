# uvicorn services.user_management.main:app --host 0.0.0.0 --port 8000 --reload

import os
import sys
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
        "Mock implementation of SafeRoute backend APIs based on the architecture spec. "
        "Endpoints return example / in-memory stub data for interactive testing via /docs."
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

# ========= Auth Token TTL =========
AUTH_TOKEN_TTL = 3600  # 1 hour in seconds

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


@app.post(
    "/v1/users/register",
    response_model=RegisterResponse,
    tags=["User Management"],
    summary="Register a new user",
)
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

    # 3) 内存里存一份（兼容你原来的结构，可以以后慢慢删）
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
    await db.commit()

    # 3) 更新内存（可选，将来可以删）
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


# async def login(payload: LoginRequest):
#     existing_id = None
#     user_data = None
#     user_found = False  # Track if user exists (regardless of password)

#     # Try to get user from Redis cache first
#     if redis_client.is_connected():
#         # Look up user_id by email
#         cached_user_id = redis_client.get(_user_email_cache_key(payload.email))
#         if cached_user_id:
#             user_found = True  # User exists
#             # Get user data from cache
#             cached_user = redis_client.get_json(_user_cache_key(cached_user_id))
#             if cached_user:
#                 # Verify password hash
#                 if cached_user.get("password_hash") == payload.password_hash:
#                     existing_id = cached_user_id
#                     user_data = cached_user

#     # Fallback to in-memory storage
#     if not existing_id:
#         for uid, u in users.items():
#             if u["email"] == payload.email:
#                 user_found = True  # User exists
#                 # Verify password hash if stored
#                 if u.get("password_hash") == payload.password_hash:
#                     existing_id = uid
#                     user_data = u
#                     break

#     # If user found but password doesn't match, return error
#     if user_found and not existing_id:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid email or password",
#         )

#     # If user not found, return error (don't auto-register for security)
#     if not user_found:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid email or password",  # Same message to prevent user enumeration
#         )

#     now = datetime.utcnow()
#     token = f"atk_{uuid.uuid4().hex[:6]}"

#     # Update last_login
#     if user_data:
#         user_data["last_login"] = now.isoformat()
#         # Update in memory
#         if existing_id in users:
#             users[existing_id]["last_login"] = now
#         # Update in Redis cache
#         if redis_client.is_connected():
#             redis_client.set_json(
#                 _user_cache_key(existing_id), user_data, ttl=CACHE_TTL
#             )
#             # Cache auth token
#             auth_data = {
#                 "user_id": existing_id,
#                 "email": payload.email,
#                 "expires_in": AUTH_TOKEN_TTL,
#                 "created_at": now.isoformat(),
#             }
#             redis_client.set_json(
#                 _auth_token_cache_key(token), auth_data, ttl=AUTH_TOKEN_TTL
#             )

#     return LoginResponse(
#         user_id=existing_id,
#         status="authenticated",
#         auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
#         email=payload.email,
#         device_id=payload.device_id,
#         last_login=now,
#     )


# async def login(payload: LoginRequest):
#     existing_id = None
#     user_data = None
#     user_found = False  # Track if user exists (regardless of password)

#     # Try to get user from Redis cache first
#     if redis_client.is_connected():
#         # Look up user_id by email
#         cached_user_id = redis_client.get(_user_email_cache_key(payload.email))
#         if cached_user_id:
#             user_found = True  # User exists
#             # Get user data from cache
#             cached_user = redis_client.get_json(_user_cache_key(cached_user_id))
#             if cached_user:
#                 # Verify password hash
#                 if cached_user.get("password_hash") == payload.password_hash:
#                     existing_id = cached_user_id
#                     user_data = cached_user

#     # Fallback to in-memory storage
#     if not existing_id:
#         for uid, u in users.items():
#             if u["email"] == payload.email:
#                 user_found = True  # User exists
#                 # Verify password hash if stored
#                 if u.get("password_hash") == payload.password_hash:
#                     existing_id = uid
#                     user_data = u
#                     break

#     # If user found but password doesn't match, return error
#     if user_found and not existing_id:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid email or password",
#         )

#     # If user not found, return error (don't auto-register for security)
#     if not user_found:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Invalid email or password",  # Same message to prevent user enumeration
#         )

#     now = datetime.utcnow()
#     token = f"atk_{uuid.uuid4().hex[:6]}"

#     # Update last_login
#     if user_data:
#         user_data["last_login"] = now.isoformat()
#         # Update in memory
#         if existing_id in users:
#             users[existing_id]["last_login"] = now
#         # Update in Redis cache
#         if redis_client.is_connected():
#             redis_client.set_json(
#                 _user_cache_key(existing_id), user_data, ttl=CACHE_TTL
#             )
#             # Cache auth token
#             auth_data = {
#                 "user_id": existing_id,
#                 "email": payload.email,
#                 "expires_in": AUTH_TOKEN_TTL,
#                 "created_at": now.isoformat(),
#             }
#             redis_client.set_json(
#                 _auth_token_cache_key(token), auth_data, ttl=AUTH_TOKEN_TTL
#             )

#     return LoginResponse(
#         user_id=existing_id,
#         status="authenticated",
#         auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
#         email=payload.email,
#         device_id=payload.device_id,
#         last_login=now,
#     )


@app.post(
    "/v1/users/{user_id}/preferences",
    response_model=PreferencesResponse,
    tags=["User Management"],
)
async def save_preferences(user_id: str, payload: PreferencesRequest):
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
            datetime.fromisoformat(user_data["last_login"])
            if user_data.get("last_login")
            else None
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
