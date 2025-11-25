import time
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Histogram,
                               generate_latest)
from pydantic import BaseModel

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

# ========= Metrics =========

SERVICE_NAME = "user_management"

# Generic per-request counter: can be shared across all services
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
)

# Request latency histogram per path
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
)

# Business metric: total user registrations
USER_REGISTRATION_TOTAL = Counter(
    "user_registrations_total",
    "Total user registrations",
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


# ========= 1. User Management =========


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
    return {"status": "ok", "service": "user_management"}


@app.post(
    "/v1/users/register", response_model=RegisterResponse, tags=["User Management"]
)
async def register_user(payload: RegisterRequest):
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    users[user_id] = {
        "user_id": user_id,
        "email": payload.email,
        "phone": payload.phone,
        "name": payload.name,
        "device_id": payload.device_id,
        "created_at": now,
        "last_login": None,
    }

    # Business metric: bump registrations counter
    USER_REGISTRATION_TOTAL.inc()

    return RegisterResponse(
        user_id=user_id,
        status="created",
        auth=AuthInfo(token=f"atk_{uuid.uuid4().hex[:6]}"),
        email=payload.email,
        phone=payload.phone,
        name=payload.name,
        device_id=payload.device_id,
        created_at=now,
    )


@app.post("/v1/auth/login", response_model=LoginResponse, tags=["User Management"])
async def login(payload: LoginRequest):
    existing_id = None
    for uid, u in users.items():
        if u["email"] == payload.email:
            existing_id = uid
            break
    if not existing_id:
        reg = await register_user(
            RegisterRequest(
                email=payload.email,
                password_hash=payload.password_hash,
                device_id=payload.device_id,
            )
        )
        existing_id = reg.user_id
    now = datetime.utcnow()
    users[existing_id]["last_login"] = now
    return LoginResponse(
        user_id=existing_id,
        status="authenticated",
        auth=AuthInfo(token=f"atk_{uuid.uuid4().hex[:6]}"),
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
    users.setdefault(user_id, {"user_id": user_id})
    users[user_id]["preferences"] = payload.dict()
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
    u = users.get(user_id)
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
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
