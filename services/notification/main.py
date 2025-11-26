# Run:
# uvicorn services.notification.main:app --host 0.0.0.0 --port 20001 --reload
# Docs: http://127.0.0.1:20001/docs

import os
import sys
import uuid
from datetime import datetime
from typing import Dict, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.twilio_client import get_twilio_client
from libs.service_urls import SOS_SERVICE_URL

app = FastAPI(
    title="Notification Service",
    version="1.0.0",
    description="Create and check SOS notifications (SMS/Call).",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NOTIFICATIONS = {}


class Location(BaseModel):
    lat: float
    lon: float
    accuracy_m: Optional[float] = None


class SOSContact(BaseModel):
    name: str
    phone: str


class SOSNotificationRequest(BaseModel):
    sos_id: str
    user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    call_number: str
    message_template: str
    variables: Dict[str, str]


class CreateResp(BaseModel):
    notification_id: str
    status: Literal["queued", "sending", "delivered", "failed"]


class StatusResult(BaseModel):
    sms_status: Literal["queued", "sending", "sent", "delivered", "failed", "not_triggered"]
    push_status: Literal["sent", "failed", "not_triggered"]
    call_status: Literal["queued", "calling", "answered", "failed", "not_triggered"]


class StatusResp(BaseModel):
    notification_id: str
    sos_id: str
    status: Literal["queued", "sending", "delivered", "failed", "partial"]
    results: StatusResult
    created_at: datetime
    updated_at: datetime


class TestSMSRequest(BaseModel):
    to_phone: str
    message: str


class TestSMSResponse(BaseModel):
    status: Literal["sent", "failed"]
    sid: Optional[str] = None
    to: str
    message: str
    error: Optional[str] = None


@app.get("/")
async def root():
    return {"service": "notification", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification"}


async def send_push_notification(user_id: str, message: str, location: Optional[Location]) -> dict:
    """
    Dummy implementation for push notification.
    In production, this would integrate with FCM/APNs.
    """
    print(f"[PUSH NOTIFICATION - DUMMY]")
    print(f"  To User: {user_id}")
    print(f"  Message: {message}")
    if location:
        print(f"  Location: {location.lat}, {location.lon}")
    print(f"  Status: Would be sent in production")
    
    # Simulate success
    return {
        "status": "sent",
        "push_id": f"push_{uuid.uuid4().hex[:8]}",
        "platform": "dummy"
    }


async def send_sms_via_sos_service(body: SOSNotificationRequest) -> dict:
    """
    Call the SOS service to send SMS via Twilio.
    """
    payload = {
        "sos_id": body.sos_id,
        "user_id": body.user_id,
        "location": body.location.dict() if body.location else None,
        "emergency_contact": body.emergency_contact.dict(),
        "message_template": body.message_template,
        "variables": body.variables
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{SOS_SERVICE_URL}/v1/emergency/sms",
                json=payload
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        print(f"Error calling SOS service: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to send SMS via SOS service: {str(e)}"
        )


@app.post("/v1/notifications/sos", response_model=CreateResp)
async def create_sos(body: SOSNotificationRequest):
    """
    Create SOS notification - sends push notification (dummy) and SMS (via SOS service).
    The push notification is a dummy implementation for now.
    The SMS is sent by calling the SOS service's /v1/emergency/sms endpoint.
    """
    nid = f"ntf_{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    
    sms_status = "not_triggered"
    push_status = "not_triggered"
    notification_status = "queued"
    
    # 1. Send push notification (dummy implementation)
    try:
        push_result = await send_push_notification(
            user_id=body.user_id,
            message=body.message_template,
            location=body.location
        )
        push_status = "sent" if push_result["status"] == "sent" else "failed"
        print(f"✓ Push notification: {push_status}")
    except Exception as e:
        print(f"✗ Push notification failed: {e}")
        push_status = "failed"
    
    # 2. Send SMS via SOS service
    try:
        sms_result = await send_sms_via_sos_service(body)
        sms_status = sms_result.get("status", "failed")
        print(f"✓ SMS via SOS service: {sms_status} (SID: {sms_result.get('sms_id', 'N/A')})")
        
        if sms_status == "sent":
            notification_status = "delivered"
        else:
            notification_status = "partial"
    except Exception as e:
        print(f"✗ SMS failed: {e}")
        sms_status = "failed"
        notification_status = "partial" if push_status == "sent" else "failed"
    
    NOTIFICATIONS[nid] = {
        "notification_id": nid,
        "sos_id": body.sos_id,
        "status": notification_status,
        "results": {
            "sms_status": sms_status,
            "push_status": push_status,
            "call_status": "not_triggered"
        },
        "created_at": now,
        "updated_at": now,
    }
    return CreateResp(notification_id=nid, status=notification_status)


@app.get("/v1/notifications/{notification_id}", response_model=StatusResp)
async def get_status(notification_id: str):
    ntf = NOTIFICATIONS.get(notification_id)
    now = datetime.utcnow()
    if not ntf:
        ntf = {
            "notification_id": notification_id,
            "sos_id": "SOS-demo",
            "status": "delivered",
            "results": {
                "sms_status": "delivered",
                "push_status": "sent",
                "call_status": "not_triggered"
            },
            "created_at": now,
            "updated_at": now,
        }
    return StatusResp(**ntf)


@app.post("/v1/test/sms", response_model=TestSMSResponse)
async def test_sms(body: TestSMSRequest):
    """
    Test endpoint to send an SMS to a phone number using Twilio.
    
    Phone number must be in E.164 format (e.g., +1234567890)
    """
    try:
        twilio = get_twilio_client()
        result = twilio.send_sms(
            to_phone=body.to_phone,
            message=body.message
        )
        
        return TestSMSResponse(
            status=result["status"],
            sid=result["sid"],
            to=result["to"],
            message=body.message,
            error=result.get("error")
        )
    except ValueError as e:
        # Twilio not configured
        raise HTTPException(
            status_code=500,
            detail=f"Twilio configuration error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to send SMS: {str(e)}"
        )
