# Run:
# uvicorn services.sos.main:app --host 0.0.0.0 --port 20006 --reload
# Docs: http://127.0.0.1:20006/docs

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from typing import Literal, Optional, Union

import httpx
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit
from libs.cas_logger import Op, cas_log
from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.two_pc import (
    P_ABORTED,
    P_COMMITTED,
    P_PREPARED,
    AbortRequest,
    AbortResponse,
    CommitRequest,
    CommitResponse,
    PrepareRequest,
    PrepareResponse,
    ensure_participant_tx_table,
)
from models.emergency import Emergency
from models.user_models import (  # noqa: F401  (registers `saferoute.users` for FK resolution)
    User,
)

logger = logging.getLogger(__name__)


def _trace_headers() -> dict:
    tid = trace_id_var.get("")
    return {TRACE_HEADER: tid} if tid else {}


# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from common.constants import QUEUE_SOS_NOTIFICATION
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.rabbitmq import RabbitMQClient
from libs.service_urls import COORDINATOR_SERVICE_URL, NOTIFICATION_SERVICE_URL
from libs.trace_context import TRACE_HEADER, trace_id_var

# RabbitMQ client (shared for the lifetime of this process)
_mq = RabbitMQClient()

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


SOS_PARTICIPANT_TABLE = "sos_participant_tx"


@app.on_event("startup")
async def _startup():
    await _mq.connect()
    # Ensure 2PC participant table exists. Best-effort: a missing DB at
    # startup must not stop the service from booting (matches outbox pattern).
    try:
        connection = db_factory.get_connection(DatabaseType.POSTGRES)
        async with connection.session_maker() as session:
            await ensure_participant_tx_table(session, SOS_PARTICIPANT_TABLE)
    except Exception:
        logger.exception("Failed to ensure sos_participant_tx table on startup")


@app.on_event("shutdown")
async def _shutdown():
    await _mq.close()


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
    SOS call via 2-Phase Commit through the coordinator service.

    The coordinator orchestrates prepare/commit across the SOS and
    notification participants. There is no direct-to-MQ fallback: the
    durability + cross-service agreement guarantees of 2PC are part of
    the SOS contract, so a missing coordinator must surface as a 503.
    """
    await cas_log.begin(Op.EMERGENCY_CALL, {"user_id": body.user_id})
    await cas_log.transition(Op.EMERGENCY_CALL, "INIT", "NOTIFICATION_REQUESTED")

    payload = body.model_dump(mode="json")

    # Tight retries with backoff. Total worst-case ~1.4s before giving up,
    # so a slow/dead coordinator does not wedge the SOS endpoint.
    last_err: Optional[Exception] = None
    data: dict = {}
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{COORDINATOR_SERVICE_URL}/v1/coordinator/sos/2pc/call",
                    json=payload,
                    headers=_trace_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                last_err = None
                break
        except httpx.HTTPError as e:
            last_err = e
            await asyncio.sleep(0.2 * (2**attempt))

    if last_err is not None:
        await cas_log.transition(
            Op.EMERGENCY_CALL,
            "NOTIFICATION_REQUESTED",
            "NOTIFICATION_FAILED",
            {"error": str(last_err)},
        )
        logger.exception("2PC SOS call failed via coordinator")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to commit SOS emergency call via 2PC: {str(last_err)}",
        )

    await cas_log.transition(
        Op.EMERGENCY_CALL,
        "NOTIFICATION_REQUESTED",
        "NOTIFICATION_SENT",
        {"call_status": data.get("status", "failed")},
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

    sms_payload = body.model_dump(mode="json")
    sms_payload["type"] = "sms"

    published = await _mq.publish(QUEUE_SOS_NOTIFICATION, sms_payload)
    data: dict = {}

    if published:
        # Message queued — return an optimistic response immediately.
        generated_sms_id = str(uuid.uuid4())
        data = {
            "emergency_id": parsed_sos_id,
            "status": "sent",
            "sms_id": generated_sms_id,
            "timestamp": datetime.utcnow().isoformat(),
            "message_sent": "",
            "recipient": body.emergency_contact.phone,
        }
    else:
        # RabbitMQ unavailable — fall back to direct HTTP call.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{NOTIFICATION_SERVICE_URL}/v1/notifications/sos/sms",
                    json=sms_payload,
                )
                try:
                    data = response.json()
                except Exception:
                    data = {}

                data["emergency_id"] = parsed_sos_id

                if not response.is_success and "status" not in data:
                    response.raise_for_status()
        except httpx.HTTPError as e:
            logger.exception(
                "Notification service SMS call failed for sos_id=%s user_id=%s recipient=%s: %s",
                body.sos_id,
                body.user_id,
                body.emergency_contact.phone,
                repr(e),
            )
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


# ===================== 2PC Participant Endpoints =====================
#
# These endpoints make the SOS service a participant in the coordinator's
# Two-Phase Commit protocol for SOS emergency calls. They are designed to
# be idempotent on tx_id so the coordinator's recovery loop can safely
# replay commit/abort after a crash.


def _row_to_state(row) -> Optional[str]:
    return row[0] if row is not None else None


@app.post("/v1/sos/2pc/prepare", response_model=PrepareResponse)
async def sos_2pc_prepare(req: PrepareRequest, db: AsyncSession = Depends(get_db)):
    """
    Phase 1: validate the payload and durably reserve the tx slot.

    Vote YES => we have written a PREPARED row and promise to be able to
    commit if asked. Vote NO => the coordinator must abort the whole tx.
    """
    # Idempotency: if we have already seen this tx_id, return the same vote.
    existing = (
        await db.execute(
            text("SELECT state FROM saferoute.sos_participant_tx WHERE tx_id = :tx_id"),
            {"tx_id": str(req.tx_id)},
        )
    ).first()
    state = _row_to_state(existing)
    if state in (P_PREPARED, P_COMMITTED):
        return PrepareResponse(tx_id=req.tx_id, vote="YES")
    if state == P_ABORTED:
        return PrepareResponse(tx_id=req.tx_id, vote="NO", reason="already aborted")

    # Validate the payload — invalid payload is a legitimate NO vote.
    try:
        EmergencyCallRequest(**req.payload)
    except Exception as e:
        return PrepareResponse(tx_id=req.tx_id, vote="NO", reason=f"invalid payload: {e}")

    expires_at = datetime.utcnow() + timedelta(seconds=req.expires_in_seconds)
    try:
        await db.execute(
            text("""
                INSERT INTO saferoute.sos_participant_tx
                    (tx_id, state, payload, expires_at)
                VALUES (:tx_id, :state, CAST(:payload AS JSONB), :expires_at)
                """),
            {
                "tx_id": str(req.tx_id),
                "state": P_PREPARED,
                "payload": json.dumps(req.payload),
                "expires_at": expires_at,
            },
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.exception("sos 2pc prepare failed")
        return PrepareResponse(tx_id=req.tx_id, vote="NO", reason=str(e))

    return PrepareResponse(tx_id=req.tx_id, vote="YES")


@app.post("/v1/sos/2pc/commit", response_model=CommitResponse)
async def sos_2pc_commit(req: CommitRequest, db: AsyncSession = Depends(get_db)):
    """
    Phase 2 commit: materialize the real Emergency row and flip state.

    Idempotent: a second commit on an already-COMMITTED tx returns the
    previously-recorded emergency_id without writing again.
    """
    row = (
        await db.execute(
            text(
                "SELECT state, payload, result FROM saferoute.sos_participant_tx "
                "WHERE tx_id = :tx_id FOR UPDATE"
            ),
            {"tx_id": str(req.tx_id)},
        )
    ).first()

    if row is None:
        raise HTTPException(status_code=404, detail="unknown tx_id")

    state, payload, result = row[0], row[1], row[2]

    if state == P_COMMITTED:
        return CommitResponse(tx_id=req.tx_id, status="committed", result=result or {})
    if state != P_PREPARED:
        raise HTTPException(status_code=409, detail=f"cannot commit from state {state}")

    emergency_id = uuid.uuid4()
    try:
        db.add(
            Emergency(
                emergency_id=emergency_id,
                user_id=payload["user_id"],
                route_id=_uuid_or_none(payload.get("route_id")),
                lat=payload["lat"],
                lon=payload["lon"],
                trigger_type=payload["trigger_type"],
                messaging_id=None,
                message=f"SOS {payload['trigger_type']}",
            )
        )
        result_obj = {
            "emergency_id": str(emergency_id),
            "call_id": f"CALL-{emergency_id}",
        }
        await db.execute(
            text("""
                UPDATE saferoute.sos_participant_tx
                SET state = :state,
                    result = CAST(:result AS JSONB),
                    updated_at = NOW()
                WHERE tx_id = :tx_id
                """),
            {
                "state": P_COMMITTED,
                "result": json.dumps(result_obj),
                "tx_id": str(req.tx_id),
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("sos 2pc commit failed")
        raise HTTPException(status_code=500, detail="commit failed")

    SOS_CALLS_TOTAL.inc()
    STATUS[str(emergency_id)] = {
        "emergency_id": emergency_id,
        "call_status": "initiated",
        "sms_status": "not_sent",
        "last_update": datetime.utcnow(),
    }
    return CommitResponse(tx_id=req.tx_id, status="committed", result=result_obj)


@app.post("/v1/sos/2pc/abort", response_model=AbortResponse)
async def sos_2pc_abort(req: AbortRequest, db: AsyncSession = Depends(get_db)):
    """
    Phase 2 abort: drop the tentative reservation. Idempotent.

    Presumed-abort: if we have never heard of this tx_id, we still return
    OK (and write a tombstone) so the coordinator can retire it.
    """
    row = (
        await db.execute(
            text(
                "SELECT state FROM saferoute.sos_participant_tx " "WHERE tx_id = :tx_id FOR UPDATE"
            ),
            {"tx_id": str(req.tx_id)},
        )
    ).first()

    if row is None:
        # Presumed-abort tombstone so we never accept a late prepare for this id.
        await db.execute(
            text("""
                INSERT INTO saferoute.sos_participant_tx
                    (tx_id, state, payload, expires_at)
                VALUES (:tx_id, :state, CAST('{}' AS JSONB), NOW())
                ON CONFLICT (tx_id) DO NOTHING
                """),
            {"tx_id": str(req.tx_id), "state": P_ABORTED},
        )
        await db.commit()
        return AbortResponse(tx_id=req.tx_id, status="aborted")

    state = row[0]
    if state == P_COMMITTED:
        # Cannot un-commit. This is a coordinator bug — surface it loudly.
        raise HTTPException(status_code=409, detail="cannot abort an already-committed tx")
    if state == P_ABORTED:
        return AbortResponse(tx_id=req.tx_id, status="aborted")

    await db.execute(
        text(
            "UPDATE saferoute.sos_participant_tx "
            "SET state = :state, updated_at = NOW() WHERE tx_id = :tx_id"
        ),
        {"state": P_ABORTED, "tx_id": str(req.tx_id)},
    )
    await db.commit()
    return AbortResponse(tx_id=req.tx_id, status="aborted")


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
