from fastapi import FastAPI, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import List, Optional, Dict, Literal
from datetime import datetime
import uuid

app = FastAPI(
    title="SafeRoute API (Mock)",
    description=(
        "Mock implementation of SafeRoute backend APIs based on the architecture spec. "
        "Endpoints return example / in-memory stub data for interactive testing via /docs."
    ),
    version="1.0.0",
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

# ========= 共用模型 =========

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

@app.post("/v1/users/register", response_model=RegisterResponse, tags=["User Management"])
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

@app.post("/v1/users/{user_id}/preferences",
          response_model=PreferencesResponse,
          tags=["User Management"])
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

@app.post("/v1/users/{user_id}/trusted-contacts",
          response_model=TrustedContactUpsertResponse,
          tags=["User Management"])
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

@app.get("/v1/users/{user_id}",
         response_model=UserResponse,
         tags=["User Management"])
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

@app.get("/v1/users/{user_id}/trusted-contacts",
         response_model=TrustedContactsListResponse,
         tags=["User Management"])
async def list_trusted_contacts(
    user_id: str,
    include_inactive: bool = Query(False, description="Mock flag; no effect in stub"),
):
    contacts = trusted_contacts.get(user_id, [])
    return TrustedContactsListResponse(
        user_id=user_id,
        contacts=[TrustedContact(**c) for c in contacts],
    )