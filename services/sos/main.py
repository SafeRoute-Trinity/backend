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
from libs.cas_logger import Op, cas_log
from libs.db import DatabaseType, get_database_factory, initialize_databases

logger = logging.getLogger(__name__)


def _trace_headers() -> dict:
    tid = trace_id_var.get("")
    return {TRACE_HEADER: tid} if tid else {}


# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.service_urls import COORDINATOR_SERVICE_URL, NOTIFICATION_SERVICE_URL
from libs.trace_context import TRACE_HEADER, trace_id_var

# Create service configuration
service_config = ServiceAppConfig(
    title="SOS Service",
    description="Emergency call/SMS/status APIs.",
    service_name="sos",
    cors_config=CORSMiddlewareConfig(),
)

# Initialize database connections
initialize_databases([DatabaseType.POSTGRES])

# Get database session dependencies
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
    user_id: str
    route_id: Optional[uuid.UUID] = None
    lat: float
    lon: float
    trigger_type: Literal["manual", "automatic"]
    phone: Optional[str] = None  # E.164 format, e.g. +353831234567


class EmergencyCallResponse(BaseModel):
    emergency_id: uuid.UUID
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
    sos_id: str  # SOS/emergency identifier (UUID string) for status correlation
    user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    message_template: Optional[str] = None
    variables: dict[str, str]
    notification_type: Optional[str] = "sos"
    locale: Optional[str] = "en"


class EmergencySMSResponse(BaseModel):
    emergency_id: uuid.UUID
    status: Literal["sent", "failed"]
    sms_id: str
    timestamp: datetime
    message_sent: str
    recipient: str


class EmergencyStatusResponse(BaseModel):
    emergency_id: uuid.UUID
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
async def call(body: EmergencyCallRequest):
    """
    Coordinator-backed SOS call:
      - coordinator atomically writes Emergency + outbox event
      - notification worker later consumes the outbox and places the call
    """
    await cas_log.begin(Op.EMERGENCY_CALL, {"user_id": body.user_id})

    payload = body.model_dump(mode="json")

    await cas_log.transition(Op.EMERGENCY_CALL, "INIT", "NOTIFICATION_REQUESTED")
    data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{COORDINATOR_SERVICE_URL}/v1/coordinator/sos/call",
                json=payload,
                headers=_trace_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            await cas_log.transition(
                Op.EMERGENCY_CALL,
                "NOTIFICATION_REQUESTED",
                "NOTIFICATION_SENT",
                {"call_status": data.get("status", "failed")},
            )
    except httpx.HTTPError as e:
        await cas_log.transition(
            Op.EMERGENCY_CALL,
            "NOTIFICATION_REQUESTED",
            "NOTIFICATION_FAILED",
            {"error": str(e)},
        )
        logger.exception("Coordinator call failed for SOS emergency")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to queue SOS emergency call: {str(e)}",
        )

    emergency_id = data.get("emergency_id")
    call_status = data.get("status", "failed")
    SOS_CALLS_TOTAL.inc()

    # Ensure keying matches /v1/emergency/{emergency_id}/status (path param is str).
    STATUS[str(emergency_id)] = {
        "emergency_id": emergency_id,
        "call_status": call_status,
        "sms_status": "not_sent",
        "last_update": datetime.utcnow(),
    }

    await cas_log.transition(Op.EMERGENCY_CALL, "NOTIFICATION_SENT", "COMMITTED")

    raw_ts = data.get("timestamp")
    if isinstance(raw_ts, datetime):
        resp_ts = raw_ts
    elif isinstance(raw_ts, str):
        try:
            resp_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            resp_ts = datetime.utcnow()
    else:
        resp_ts = datetime.utcnow()

    raw_status = data.get("status", "initiated")
    resp_status = raw_status if raw_status in ("initiated", "failed") else "initiated"

    return EmergencyCallResponse(
        emergency_id=emergency_id,
        status=resp_status,
        call_id=str(data.get("call_id") or data.get("sid") or ""),
        timestamp=resp_ts,
    )


@app.post("/v1/emergency/sms", response_model=EmergencySMSResponse)
async def sms(body: EmergencySMSRequest, db: AsyncSession = Depends(get_db)):
    """
    Send emergency SMS with rich details (templates, variables, location).
    Delegates delivery to the Notification service.
    """
    await cas_log.begin(Op.EMERGENCY_SMS, {"sos_id": body.sos_id, "user_id": body.user_id})
    # Validate sos_id is UUID (user_id is now a plain string from Auth0)
    parsed_sos_id = _uuid_or_none(body.sos_id)
    if parsed_sos_id is None:
        raise HTTPException(status_code=400, detail="sos_id must be a valid UUID")

    await cas_log.transition(Op.EMERGENCY_SMS, "INIT", "VALIDATED", {"sos_id": body.sos_id})
    await cas_log.transition(Op.EMERGENCY_SMS, "VALIDATED", "NOTIFICATION_REQUESTED")

    data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{NOTIFICATION_SERVICE_URL}/v1/notifications/sos/sms",
                json=body.model_dump(mode="json"),
                headers=_trace_headers(),
            )
            try:
                data = response.json()
            except Exception:
                data = {}

            data["emergency_id"] = parsed_sos_id

            # Only raise if the response has no usable SMS data
            if not response.is_success and "status" not in data:
                response.raise_for_status()
            await cas_log.transition(
                Op.EMERGENCY_SMS,
                "NOTIFICATION_REQUESTED",
                "SMS_SENT",
                {"sms_id": data.get("sms_id")},
            )
    except httpx.HTTPError as e:
        await cas_log.transition(
            Op.EMERGENCY_SMS,
            "NOTIFICATION_REQUESTED",
            "NOTIFICATION_FAILED",
            {"error": str(e)},
        )
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
            pass

        await cas_log.transition(Op.EMERGENCY_SMS, "NOTIFICATION_FAILED", "FAILED")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to send SMS via notification service: {str(e)}",
        )

    # Update status
    now = datetime.utcnow()
    s = STATUS.setdefault(
        str(parsed_sos_id),
        {
            "emergency_id": parsed_sos_id,
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

    await cas_log.transition(Op.EMERGENCY_SMS, "SMS_SENT", "COMMITTED")
    return EmergencySMSResponse(
        emergency_id=parsed_sos_id,
        status=data.get("status", "failed"),
        sms_id=str(_uuid_or_none(data.get("sms_id", "")) or uuid.uuid4()),
        timestamp=data.get("timestamp", datetime.utcnow().isoformat()),
        message_sent=data.get("message_sent", ""),
        recipient=data.get("recipient", body.emergency_contact.phone),
    )


@app.get("/v1/emergency/{emergency_id}/status", response_model=EmergencyStatusResponse)
async def get_status(emergency_id: str = Path(..., description="SOS event to check")):
    # Validate emergency_id is UUID
    parsed_emergency_id = _uuid_or_none(emergency_id)
    if parsed_emergency_id is None:
        raise HTTPException(status_code=400, detail="emergency_id must be a valid UUID")

    now = datetime.utcnow()
    s = STATUS.get(
        emergency_id,
        {
            "emergency_id": emergency_id,
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
                f"{NOTIFICATION_SERVICE_URL}/v1/test/sms",
                json=body.model_dump(),
                headers=_trace_headers(),
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
