import os
import sys
import time
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

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
    """Health check endpoint with Redis status."""
    redis_status = "connected" if redis_client.is_connected() else "disconnected"
    return {"status": "ok", "service": "user_management", "redis": redis_status}


@app.post(
    "/v1/users/register", response_model=RegisterResponse, tags=["User Management"]
)
async def register_user(payload: RegisterRequest):
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # Prepare user data
    user_data = {
        "user_id": user_id,
        "email": payload.email,
        "phone": payload.phone,
        "name": payload.name,
        "device_id": payload.device_id,
        "password_hash": payload.password_hash,  # Store password hash for authentication
        "created_at": now.isoformat(),
        "last_login": None,
    }

    # Store in memory (fallback)
    users[user_id] = user_data.copy()
    users[user_id]["created_at"] = now
    users[user_id]["last_login"] = None

    # Cache in Redis
    if redis_client.is_connected():
        # Cache user data by user_id
        redis_client.set_json(_user_cache_key(user_id), user_data, ttl=CACHE_TTL)
        # Cache user_id lookup by email
        redis_client.set(_user_email_cache_key(payload.email), user_id, ttl=CACHE_TTL)
        # Cache auth token
        auth_data = {
            "user_id": user_id,
            "email": payload.email,
            "expires_in": AUTH_TOKEN_TTL,
            "created_at": now.isoformat(),
        }
        redis_client.set_json(
            _auth_token_cache_key(token), auth_data, ttl=AUTH_TOKEN_TTL
        )

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
async def login(payload: LoginRequest):
    existing_id = None
    user_data = None
    user_found = False  # Track if user exists (regardless of password)

    # Try to get user from Redis cache first
    if redis_client.is_connected():
        # Look up user_id by email
        cached_user_id = redis_client.get(_user_email_cache_key(payload.email))
        if cached_user_id:
            user_found = True  # User exists
            # Get user data from cache
            cached_user = redis_client.get_json(_user_cache_key(cached_user_id))
            if cached_user:
                # Verify password hash
                if cached_user.get("password_hash") == payload.password_hash:
                    existing_id = cached_user_id
                    user_data = cached_user

    # Fallback to in-memory storage
    if not existing_id:
        for uid, u in users.items():
            if u["email"] == payload.email:
                user_found = True  # User exists
                # Verify password hash if stored
                if u.get("password_hash") == payload.password_hash:
                    existing_id = uid
                    user_data = u
                    break

    # If user found but password doesn't match, return error
    if user_found and not existing_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # If user not found, return error (don't auto-register for security)
    if not user_found:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",  # Same message to prevent user enumeration
        )

    now = datetime.utcnow()
    token = f"atk_{uuid.uuid4().hex[:6]}"

    # Update last_login
    if user_data:
        user_data["last_login"] = now.isoformat()
        # Update in memory
        if existing_id in users:
            users[existing_id]["last_login"] = now
        # Update in Redis cache
        if redis_client.is_connected():
            redis_client.set_json(
                _user_cache_key(existing_id), user_data, ttl=CACHE_TTL
            )
            # Cache auth token
            auth_data = {
                "user_id": existing_id,
                "email": payload.email,
                "expires_in": AUTH_TOKEN_TTL,
                "created_at": now.isoformat(),
            }
            redis_client.set_json(
                _auth_token_cache_key(token), auth_data, ttl=AUTH_TOKEN_TTL
            )

    return LoginResponse(
        user_id=existing_id,
        status="authenticated",
        auth=AuthInfo(token=token, expires_in=AUTH_TOKEN_TTL),
        email=payload.email,
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
