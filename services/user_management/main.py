# Run:
# uvicorn services.user_management.main:app --host 0.0.0.0 --port 20000 --reload
# Docs: http://127.0.0.1:20000/docs


import logging
import os
import sys
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
# for postgresql
from sqlalchemy import func, select
from sqlalchemy import func 
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from libs.db import get_db
from models.audit import Audit
from models.user_models import User

# Add parent directory to path to import libs
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from libs.redis_client import get_redis_client

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

# ========= Redis Cache Configuration =========
# Cache TTL in seconds (default: 1 hour = 3600 seconds)
CACHE_TTL = int(os.getenv("REDIS_CACHE_TTL", "3600"))
# Cache TTL for auth tokens (default: 1 hour)
AUTH_TOKEN_TTL = int(os.getenv("REDIS_AUTH_TOKEN_TTL", "3600"))

# Initialize Redis client
redis_client = get_redis_client()


# ========= Cache Key Helpers =========
def _user_cache_key(user_id: str) -> str:
    """Generate cache key for user data."""
    return f"user:{user_id}"


def _user_email_cache_key(email: str) -> str:
    """Generate cache key for user lookup by email."""
    return f"user:email:{email.lower()}"


def _auth_token_cache_key(token: str) -> str:
    """Generate cache key for auth token."""
    return f"auth:token:{token}"


# ========= In-memory mock storage (fallback) =========
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


# ========= Audit Log Models =========
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
    """Health check endpoint with Redis status."""
    redis_status = "connected" if redis_client.is_connected() else "disconnected"
    return {"status": "ok", "service": "user_management", "redis": redis_status}


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

    # 3) 准备一份 dict 给 Redis / 内存
    user_data = {
        "user_id": user_id,
        "email": payload.email,
        "phone": payload.phone,
        "name": payload.name,
        "device_id": payload.device_id,
        "password_hash": payload.password_hash,
        "created_at": now.isoformat(),
        "last_login": None,
    }

    # 内存里存一份（兼容你原来的结构，可以以后慢慢删）
    users[user_id] = {
        **user_data,
        "created_at": now,
        "last_login": None,
    }

    # 4) 写 Redis 缓存（和你原来一样）
    if redis_client.is_connected():
        redis_client.set_json(_user_cache_key(user_id), user_data, ttl=CACHE_TTL)
        redis_client.set(_user_email_cache_key(payload.email), user_id, ttl=CACHE_TTL)

        auth_data = {
            "user_id": user_id,
            "email": payload.email,
            "expires_in": AUTH_TOKEN_TTL,
            "created_at": now.isoformat(),
        }
        redis_client.set_json(
            _auth_token_cache_key(token), auth_data, ttl=AUTH_TOKEN_TTL
        )

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


# async def register_user(payload: RegisterRequest):
#     user_id = f"usr_{uuid.uuid4().hex[:8]}"
#     now = datetime.utcnow()
#     token = f"atk_{uuid.uuid4().hex[:6]}"

#     # Prepare user data
#     user_data = {
#         "user_id": user_id,
#         "email": payload.email,
#         "phone": payload.phone,
#         "name": payload.name,
#         "device_id": payload.device_id,
#         "password_hash": payload.password_hash,  # Store password hash for authentication
#         "created_at": now.isoformat(),
#         "last_login": None,
#     }

#     # Store in memory (fallback)
#     users[user_id] = user_data.copy()
#     users[user_id]["created_at"] = now
#     users[user_id]["last_login"] = None

#     # Cache in Redis
#     if redis_client.is_connected():
#         # Cache user data by user_id
#         redis_client.set_json(_user_cache_key(user_id), user_data, ttl=CACHE_TTL)
#         # Cache user_id lookup by email
#         redis_client.set(_user_email_cache_key(payload.email), user_id, ttl=CACHE_TTL)
#         # Cache auth token
#         auth_data = {
#             "user_id": user_id,
#             "email": payload.email,
#             "expires_in": AUTH_TOKEN_TTL,
#             "created_at": now.isoformat(),
#         }
#         redis_client.set_json(
#             _auth_token_cache_key(token), auth_data, ttl=AUTH_TOKEN_TTL
#         )

#     return RegisterResponse(
#         user_id=user_id,
#         status="created",
#         auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
#         email=payload.email,
#         phone=payload.phone,
#         name=payload.name,
#         device_id=payload.device_id,
#         created_at=now,
#     )


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

    # 3) 准备缓存数据
    user_data = {
        "user_id": user.user_id,
        "email": user.email,
        "phone": user.phone,
        "name": user.name,
        "device_id": user.device_id,
        "password_hash": user.password_hash,
        "created_at": user.created_at.isoformat(),
        "last_login": now.isoformat(),
    }

    # 更新内存（可选，将来可以删）
    users[user.user_id] = {
        **user_data,
        "created_at": user.created_at,
        "last_login": now,
    }

    # 4) 更新 Redis 缓存（用户数据 + email->user_id + auth token）
    if redis_client.is_connected():
        redis_client.set_json(
            _user_cache_key(user.user_id),
            user_data,
            ttl=CACHE_TTL,
        )
        redis_client.set(
            _user_email_cache_key(user.email),
            user.user_id,
            ttl=CACHE_TTL,
        )
        auth_data = {
            "user_id": user.user_id,
            "email": user.email,
            "expires_in": AUTH_TOKEN_TTL,
            "created_at": now.isoformat(),
        }
        redis_client.set_json(
            _auth_token_cache_key(token),
            auth_data,
            ttl=AUTH_TOKEN_TTL,
        )

    return LoginResponse(
        user_id=user.user_id,
        status="authenticated",
        auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
        email=user.email,
        device_id=payload.device_id,
        last_login=now,
    )



@app.get(
    "/api/audit",
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




@app.post(
    "/v1/users/{user_id}/preferences",
    response_model=PreferencesResponse,
    tags=["User Management"],
)
async def save_preferences(user_id: str, payload: PreferencesRequest):
    now = datetime.utcnow()

    # Get user data
    user_data = None
    if redis_client.is_connected():
        user_data = redis_client.get_json(_user_cache_key(user_id))

    if not user_data:
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

    # Update in Redis cache
    if redis_client.is_connected():
        redis_client.set_json(_user_cache_key(user_id), user_data, ttl=CACHE_TTL)

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
async def get_user(user_id: str):
    u = None

    # Try to get from Redis cache first
    if redis_client.is_connected():
        cached_user = redis_client.get_json(_user_cache_key(user_id))
        if cached_user:
            u = cached_user.copy()
            # Convert ISO strings back to datetime objects
            if isinstance(u.get("created_at"), str):
                u["created_at"] = datetime.fromisoformat(u["created_at"])
            if isinstance(u.get("last_login"), str):
                u["last_login"] = datetime.fromisoformat(u["last_login"])
            # Remove password_hash from response
            u.pop("password_hash", None)

    # Fallback to in-memory storage
    if not u:
        u = users.get(user_id)
        if u:
            # Remove password_hash from response
            u = u.copy()
            u.pop("password_hash", None)

    # If still not found, create demo user
    if not u:
        now = datetime.utcnow()
        u = {
            "user_id": user_id,
            "name": "Demo User",
            "email": f"{user_id}@example.com",
            "phone": "+353800000000",
            "created_at": now,
            "last_login": now,
        }
        users[user_id] = u
        # Cache in Redis
        if redis_client.is_connected():
            user_data = u.copy()
            user_data["created_at"] = now.isoformat()
            user_data["last_login"] = now.isoformat()
            redis_client.set_json(_user_cache_key(user_id), user_data, ttl=CACHE_TTL)

    return UserResponse(**u)


@app.get("/api/audit", tags=["Audit"])
async def api_audit(
    event_type: Optional[str] = Query(None, description="Filter by event_type"),
    user_id: Optional[str] = Query(None, description="Filter by user_id (UUID)"),
    event_id: Optional[str] = Query(None, description="Filter by event_id (UUID)"),
    q: Optional[str] = Query(
        None, description="Fulltext search over message (case-insensitive)"
    ),
    start: Optional[datetime] = Query(None, description="Start datetime (ISO)"),
    end: Optional[datetime] = Query(None, description="End datetime (ISO)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Simple audit query endpoint for admin/front-end use.
    Returns paginated audit records from schema saferoute.audit.
    """
    # Build filters
    filters = []
    if event_type:
        filters.append(Audit.event_type == event_type)

    import uuid as _uuid

    if user_id:
        try:
            uid = _uuid.UUID(user_id)
            filters.append(Audit.user_id == uid)
        except Exception:
            raise HTTPException(status_code=400, detail="user_id must be a valid UUID")

    if event_id:
        try:
            eid = _uuid.UUID(event_id)
            filters.append(Audit.event_id == eid)
        except Exception:
            raise HTTPException(status_code=400, detail="event_id must be a valid UUID")

    if start:
        filters.append(Audit.created_at >= start)
    if end:
        filters.append(Audit.created_at <= end)
    if q:
        filters.append(Audit.message.ilike(f"%{q}%"))

    try:
        # total count
        count_stmt = (
            select(func.count()).select_from(Audit).where(*filters)
            if filters
            else select(func.count()).select_from(Audit)
        )
        total_res = await db.execute(count_stmt)
        total = int(total_res.scalar() or 0)

        stmt = select(Audit).where(*filters) if filters else select(Audit)
        stmt = (
            stmt.order_by(Audit.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        res = await db.execute(stmt)
        rows = res.scalars().all()

        items = []
        for r in rows:
            items.append(
                {
                    "log_id": str(r.log_id) if r.log_id else None,
                    "user_id": str(r.user_id) if r.user_id else None,
                    "event_type": r.event_type,
                    "event_id": str(r.event_id) if r.event_id else None,
                    "message": r.message,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )

        return {"items": items, "page": page, "per_page": per_page, "total": total}
    except Exception as exc:
        logger = logging.getLogger(__name__)
        logger.exception("Audit query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Audit query failed")


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
