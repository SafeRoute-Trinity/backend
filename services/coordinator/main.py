import logging
import os
import sys
import uuid
from datetime import datetime
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit
from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.outbox import ensure_outbox_tables
from models.emergency import Emergency
from models.outbox import OutboxEvent
from models.user_models import (  # noqa: F401 (registers `saferoute.users` for FK resolution)
    User,
)

logger = logging.getLogger(__name__)

load_dotenv()

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


class EmergencyCallRequest(BaseModel):
    user_id: str
    route_id: Optional[uuid.UUID] = None
    lat: float
    lon: float
    trigger_type: Literal["manual", "automatic"]
    phone: str  # E.164 format, e.g. +353831234567


class EmergencyCallResponse(BaseModel):
    emergency_id: uuid.UUID
    status: Literal["initiated", "failed"]
    call_id: str
    timestamp: datetime


service_config = ServiceAppConfig(
    title="SafeRoute Coordinator Service",
    description="Coordinates DB atomicity + outbox publication for multi-service workflows.",
    service_name="coordinator",
    cors_config=CORSMiddlewareConfig(),
)

initialize_databases([DatabaseType.POSTGRES])

db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)
get_serializable_db = db_factory.get_serializable_session_dependency(DatabaseType.POSTGRES)

factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

SOS_CALLS_QUEUED_TOTAL = factory.add_business_metric(
    "sos_calls_queued_total",
    "Total SOS emergency call intents queued to outbox",
)


@app.on_event("startup")
async def startup_event() -> None:
    # DDL is idempotent; for unit tests/CI this might fail if no DB is present,
    # but the service can still start (worker paths will fail gracefully later).
    try:
        connection = db_factory.get_connection(DatabaseType.POSTGRES)
        async with connection.session_maker() as session:
            await ensure_outbox_tables(session)
    except Exception:
        logger.exception("Failed to ensure outbox tables on startup")


@app.post("/v1/coordinator/sos/call", response_model=EmergencyCallResponse)
async def queue_emergency_call(
    body: EmergencyCallRequest,
    db: AsyncSession = Depends(get_serializable_db),
):
    """
    Atomic operation:
      1) Insert Emergency row (+ audit)
      2) Insert outbox event to send the emergency call via Notification service

    These happen in a single DB transaction.
    """
    now = datetime.utcnow()
    emergency_id = uuid.uuid4()

    # Correlation id; may be replaced by Twilio SID later by the worker.
    call_request_id = f"CALL-{uuid.uuid4().hex[:10]}"
    call_reason = f"SOS {body.trigger_type}"

    try:
        db.add(
            Emergency(
                emergency_id=emergency_id,
                user_id=body.user_id,
                route_id=body.route_id,
                lat=body.lat,
                lon=body.lon,
                trigger_type=body.trigger_type,
                messaging_id=None,
                message=f"SOS {body.trigger_type}",
            )
        )

        # Best-effort audit write (must not break the coordinator transaction).
        # `write_audit` uses SAVEPOINT and can fall back if DB is unavailable.
        await write_audit(
            db=db,
            event_type="emergency",
            user_id=None,
            event_id=emergency_id,
            message=f"sos_call_queued emergency_id={emergency_id} trigger_type={body.trigger_type}",
            commit=False,
        )

        db.add(
            OutboxEvent(
                event_id=uuid.uuid4(),
                event_type="sos.emergency_call",
                aggregate_id=emergency_id,
                payload={
                    "emergency_id": str(emergency_id),
                    "user_id": body.user_id,
                    "phone_number": body.phone,
                    "user_location": {"lat": body.lat, "lon": body.lon},
                    "call_reason": call_reason,
                    "call_request_id": call_request_id,
                },
                status="pending",
                attempts=0,
                max_attempts=5,
                available_at=now,
                last_error=None,
            )
        )

        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Could not queue emergency call: {str(e)}",
        )
    except Exception:
        await db.rollback()
        raise

    SOS_CALLS_QUEUED_TOTAL.inc()

    return EmergencyCallResponse(
        emergency_id=emergency_id,
        status="initiated",
        call_id=call_request_id,
        timestamp=now,
    )
