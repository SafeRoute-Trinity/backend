# Run:
# uvicorn services.sos.main:app --host 0.0.0.0 --port 20006 --reload
# Docs: http://127.0.0.1:20006/docs

import logging
import os
import sys
import uuid
from datetime import datetime
from typing import Literal, Optional, Union

import httpx
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit
from libs.db import DatabaseType, get_database_factory, initialize_databases

logger = logging.getLogger(__name__)


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

# Initialize database connections
initialize_databases([DatabaseType.POSTGRES])

# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)

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


def _uuid_or_none(val: Optional[Union[str, uuid.UUID]]):
    if val is None:
        return None
    if isinstance(val, uuid.UUID):
        return val
    if isinstance(val, str):
        try:
            return uuid.UUID(val)
        except Exception:
            return None
    return None


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


@app.post("/v1/emergency/call", response_model=EmergencyCallResponse)
async def call(body: EmergencyCallRequest, db: AsyncSession = Depends(get_db)):
    # Validate sos_id is UUID
    parsed_sos_id = _uuid_or_none(body.sos_id)
    if parsed_sos_id is None:
        raise HTTPException(status_code=400, detail="sos_id must be a valid UUID")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/notifications/sos/call",
                json=body.model_dump(),
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        # Log the httpx error details for easier debugging
        logger.exception(
            "Notification service call failed for sos_id=%s phone=%s: %s",
            body.sos_id,
            body.phone_number,
            repr(e),
        )

        await write_audit(
            db=db,
            event_type="emergency",
            user_id=None,  # call request 没 user_id 字段
            event_id=parsed_sos_id,
            message=f"sos_call_failed sos_id={body.sos_id} phone={body.phone_number} reason={body.call_reason} error={str(e)}",
            commit=True,
        )
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
    # Audit call success
    try:
        await write_audit(
            db=db,
            event_type="emergency",
            user_id=None,
            event_id=parsed_sos_id,
            message=f"sos_call_initiated sos_id={body.sos_id} phone={body.phone_number} status={data.get('status')}",
            commit=True,
        )
    except Exception:
        pass
    return EmergencyCallResponse(**data)


@app.post("/v1/emergency/sms", response_model=EmergencySMSResponse)
async def sms(body: EmergencySMSRequest, db: AsyncSession = Depends(get_db)):
    """
    Send emergency SMS with rich details (templates, variables, location).
    Delegates delivery to the Notification service.
    """
    # Validate sos_id is UUID (user_id is now a plain string from Auth0)
    parsed_sos_id = _uuid_or_none(body.sos_id)
    if parsed_sos_id is None:
        raise HTTPException(status_code=400, detail="sos_id must be a valid UUID")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/notifications/sos/sms",
                json=body.model_dump(),
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as e:
        # Log and audit SMS failure
        logger.exception(
            "Notification service SMS call failed for sos_id=%s user_id=%s recipient=%s: %s",
            body.sos_id,
            body.user_id,
            body.emergency_contact.phone,
            repr(e),
        )

        try:
            await write_audit(
                db=db,
                event_type="emergency",
                user_id=body.user_id,
                event_id=parsed_sos_id,
                message=f"sos_sms_failed sos_id={body.sos_id} user_id={body.user_id} recipient={body.emergency_contact.phone} error={str(e)}",
                commit=True,
            )
        except Exception:
            # Audit failures should not block the main error
            pass

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

    # Audit SMS success
    try:
        await write_audit(
            db=db,
            event_type="emergency",
            user_id=body.user_id,
            event_id=parsed_sos_id,
            message=f"sos_sms_sent sos_id={body.sos_id} user_id={body.user_id} sms_id={data.get('sms_id')} recipient={data.get('recipient')}",
            commit=True,
        )
    except Exception:
        # don't let audit failures affect response
        pass

    return EmergencySMSResponse(**data)


@app.get("/v1/emergency/{sos_id}/status", response_model=EmergencyStatusResponse)
async def get_status(sos_id: str = Path(..., description="SOS event to check")):
    # Validate sos_id is UUID
    parsed_sos_id = _uuid_or_none(sos_id)
    if parsed_sos_id is None:
        raise HTTPException(status_code=400, detail="sos_id must be a valid UUID")

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
async def test_sms(body: TestSMSRequest, db: AsyncSession = Depends(get_db)):
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
        # Log and audit test sms failure
        logger.exception("Notification test SMS call failed to=%s: %s", body.to_phone, repr(e))

        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=None,
                event_id=None,
                message=f"test_sms_failed to={body.to_phone} error={str(e)}",
                commit=True,
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=503,
            detail=f"Failed to send SMS via notification service: {str(e)}",
        )
