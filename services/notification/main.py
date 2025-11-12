# Run:
# uvicorn services.notification.main:app --host 0.0.0.0 --port 20001 --reload
# Docs: http://127.0.0.1:20001/docs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Literal
from datetime import datetime
import uuid

app = FastAPI(title="Notification Service", version="1.0.0",
              description="Create and check SOS notifications (SMS/Call).")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

NOTIFICATIONS = {}

class Location(BaseModel):
    lat: float; lon: float; accuracy_m: Optional[float] = None

class SOSContact(BaseModel):
    name: str; phone: str

class SOSNotificationRequest(BaseModel):
    sos_id: str; user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    call_number: str
    message_template: str
    variables: Dict[str, str]

class CreateResp(BaseModel):
    notification_id: str
    status: Literal["queued", "sending", "delivered", "failed"]

class StatusResult(BaseModel):
    sms_status: Literal["queued","sending","delivered","failed","not_triggered"]
    call_status: Literal["queued","calling","answered","failed","not_triggered"]

class StatusResp(BaseModel):
    notification_id: str; sos_id: str
    status: Literal["queued","sending","delivered","failed","partial"]
    results: StatusResult
    created_at: datetime; updated_at: datetime

@app.get("/")
async def root(): return {"service": "notification", "status": "running"}

@app.get("/health")
async def health(): return {"status": "ok", "service": "notification"}

@app.post("/v1/notifications/sos", response_model=CreateResp)
async def create_sos(body: SOSNotificationRequest):
    nid = f"ntf_{uuid.uuid4().hex[:6]}"; now = datetime.utcnow()
    NOTIFICATIONS[nid] = {"notification_id": nid, "sos_id": body.sos_id, "status": "queued",
                          "results": {"sms_status": "queued", "call_status": "not_triggered"},
                          "created_at": now, "updated_at": now}
    return CreateResp(notification_id=nid, status="queued")

@app.get("/v1/notifications/{notification_id}", response_model=StatusResp)
async def get_status(notification_id: str):
    ntf = NOTIFICATIONS.get(notification_id)
    now = datetime.utcnow()
    if not ntf:
        ntf = {"notification_id": notification_id, "sos_id": "SOS-demo", "status": "delivered",
               "results": {"sms_status": "delivered", "call_status": "not_triggered"},
               "created_at": now, "updated_at": now}
    return StatusResp(**ntf)
