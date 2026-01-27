# Run:
# uvicorn services.sos.main:app --host 0.0.0.0 --port 20006 --reload
# Docs: http://127.0.0.1:20006/docs

import os
import sys
from datetime import datetime
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException, Path
from pydantic import BaseModel

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.service_urls import NOTIFICATION_SERVICE_URL

# Create service configuration
service_config = ServiceAppConfig(
    title="SOS Service",
    description="Emergency call/SMS/status APIs.",
    service_name="sos",
    cors_config=CORSMiddlewareConfig(),
)

# Create factory and build app
factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

# Add business-specific metrics
SOS_CALLS_TOTAL = factory.add_business_metric(
    "sos_calls_total",
    "Total SOS emergency calls initiated",
)

SOS_SMS_TOTAL = factory.add_business_metric(
    "sos_sms_total",
    "Total SOS emergency SMS sent",
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
    message_template: Optional[str] = None
    variables: dict[str, str]
    notification_type: Optional[str] = "sos"
    locale: Optional[str] = "en"


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
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/notifications/sos/call",
                json=body.model_dump(),
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to dispatch SOS call via notification service: {str(e)}",
        )

    now = datetime.utcnow()
    STATUS[body.sos_id] = {
        "sos_id": body.sos_id,
        "call_status": data.get("status", "failed"),
        "sms_status": "not_sent",
        "last_update": now,
    }

    SOS_CALLS_TOTAL.inc()
    return EmergencyCallResponse(**data)


@app.post("/v1/emergency/sms", response_model=EmergencySMSResponse)
async def sms(body: EmergencySMSRequest):
    """
    Send emergency SMS with rich details (templates, variables, location).
    Delegates delivery to the Notification service.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/notifications/sos/sms",
                json=body.model_dump(),
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to send SMS via notification service: {str(e)}",
        )

    # Update status
    now = datetime.utcnow()
    s = STATUS.setdefault(
        body.sos_id,
        {
            "sos_id": body.sos_id,
            "call_status": "not_triggered",
            "sms_status": "not_sent",
            "last_update": now,
        },
    )
    s["sms_status"] = data.get("status", "failed")
    s["last_update"] = now

    # Business metric: count SOS SMS
    SOS_SMS_TOTAL.inc()

    return EmergencySMSResponse(**data)


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
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/test/sms", json=body.model_dump()
            )
            response.raise_for_status()
            return TestSMSResponse(**response.json())
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to send SMS via notification service: {str(e)}",
        )
