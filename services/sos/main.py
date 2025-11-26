# Run:
# uvicorn services.sos.main:app --host 0.0.0.0 --port 20006 --reload
# Docs: http://127.0.0.1:20006/docs

import os
import sys
import uuid
from datetime import datetime
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Path, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.twilio_client import get_twilio_client

app = FastAPI(
    title="SOS Service", version="1.0.0", description="Emergency call/SMS/status APIs."
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATUS = {}


class Point(BaseModel):
    lat: float
    lon: float


class EmergencyCallRequest(BaseModel):
    sos_id: str
    phone_number: str
    user_location: Point
    call_reason: str


class EmergencyCallResponse(BaseModel):
    status: Literal["initiated", "failed"]
    call_id: str
    timestamp: datetime


class Location(BaseModel):
    lat: float
    lon: float
    accuracy_m: Optional[float] = None


class SOSContact(BaseModel):
    name: str
    phone: str


class EmergencySMSRequest(BaseModel):
    sos_id: str
    user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    message_template: str
    variables: dict[str, str]


class EmergencySMSResponse(BaseModel):
    status: Literal["sent", "failed"]
    sms_id: str
    timestamp: datetime
    message_sent: str
    recipient: str


class EmergencyStatusResponse(BaseModel):
    sos_id: str
    call_status: Literal["initiated", "connected", "failed", "not_triggered"]
    sms_status: Literal["sent", "failed", "not_sent"]
    last_update: datetime


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
    return {"service": "sos", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "sos"}


@app.post("/v1/emergency/call", response_model=EmergencyCallResponse)
async def call(body: EmergencyCallRequest):
    cid = f"CALL-{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    STATUS[body.sos_id] = {
        "sos_id": body.sos_id,
        "call_status": "initiated",
        "sms_status": "not_sent",
        "last_update": now,
    }
    return EmergencyCallResponse(status="initiated", call_id=cid, timestamp=now)


@app.post("/v1/emergency/sms", response_model=EmergencySMSResponse)
async def sms(body: EmergencySMSRequest):
    """
    Send emergency SMS with rich details (templates, variables, location).
    This is the production SMS sender using Twilio.
    """
    now = datetime.utcnow()
    
    # Format the message with variables
    message = body.message_template
    for key, value in body.variables.items():
        message = message.replace(f"{{{key}}}", value)
    
    # Add location if provided
    if body.location:
        location_text = f"\n\nLocation: https://maps.google.com/?q={body.location.lat},{body.location.lon}"
        if body.location.accuracy_m:
            location_text += f" (±{body.location.accuracy_m}m)"
        message += location_text
    
    # Send SMS via Twilio
    try:
        twilio = get_twilio_client()
        result = twilio.send_sms(
            to_phone=body.emergency_contact.phone,
            message=message
        )
        
        if result["status"] == "sent":
            sid = result["sid"]
            sms_status = "sent"
            status = "sent"
        else:
            sid = f"SMS-{uuid.uuid4().hex[:6]}"
            sms_status = "failed"
            status = "failed"
            print(f"SMS send failed: {result.get('error')}")
            
    except Exception as e:
        print(f"Error sending SMS: {e}")
        sid = f"SMS-{uuid.uuid4().hex[:6]}"
        sms_status = "failed"
        status = "failed"
    
    # Update status
    s = STATUS.setdefault(
        body.sos_id,
        {
            "sos_id": body.sos_id,
            "call_status": "not_triggered",
            "sms_status": "not_sent",
            "last_update": now,
        },
    )
    s["sms_status"] = sms_status
    s["last_update"] = now
    
    return EmergencySMSResponse(
        status=status, 
        sms_id=sid, 
        timestamp=now,
        message_sent=message,
        recipient=body.emergency_contact.phone
    )


@app.get("/v1/emergency/{sos_id}/status", response_model=EmergencyStatusResponse)
async def get_status(sos_id: str = Path(..., description="SOS event to check")):
    now = datetime.utcnow()
    s = STATUS.get(
        sos_id,
        {
            "sos_id": sos_id,
            "call_status": "not_triggered",
            "sms_status": "not_sent",
            "last_update": now,
        },
    )
    return EmergencyStatusResponse(**s)


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
