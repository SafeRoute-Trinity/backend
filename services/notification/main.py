# Run:
# uvicorn services.notification.main:app --host 0.0.0.0 --port 20001 --reload
# Docs: http://127.0.0.1:20001/docs

import asyncio
import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit
from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.outbox import ensure_outbox_tables
from libs.twilio_client import get_twilio_client
from services.notification.manager import NotificationManager
from services.notification.schemas import (
    CreateResp,
    EmergencyCallRequest,
    EmergencyCallResponse,
    EmergencySMSRequest,
    EmergencySMSResponse,
    SOSNotificationRequest,
    StatusResp,
    TestSMSRequest,
    TestSMSResponse,
)

OUTBOX_WORKER_ENABLED = os.getenv("OUTBOX_WORKER_ENABLED", "false").lower() == "true"
OUTBOX_POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "2"))

# Initialize database connections
initialize_databases([DatabaseType.POSTGRES])

# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)

# Create service configuration
service_config = ServiceAppConfig(
    title="Notification Service",
    description="Create and check SOS notifications (SMS/Call).",
    service_name="notification",
    cors_config=CORSMiddlewareConfig(),
)

# Create factory and build app
factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

# Add business-specific metrics
NOTIFICATION_SOS_CREATED_TOTAL = factory.add_business_metric(
    "notification_sos_created_total",
    "Total number of SOS notifications created",
)

NOTIFICATION_STATUS_CHECKS_TOTAL = factory.add_business_metric(
    "notification_status_checks_total",
    "Total number of notification status lookups",
)

NOTIFICATIONS = {}
manager = NotificationManager(NOTIFICATIONS)

import logging

logger = logging.getLogger(__name__)


def _maybe_uuid(val):
    try:
        import uuid as _uuid

        if val is None:
            return None
        if isinstance(val, _uuid.UUID):
            return val
        if isinstance(val, str):
            return _uuid.UUID(val)
    except Exception:
        return None
    return None


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Return 500 with error detail so we can see the real cause."""
    tb = traceback.format_exc()
    print(f"[500] {request.method} {request.url.path}: {exc!r}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        },
    )


# ========= Routes =========


@app.get("/")
async def root():
    return {"service": "notification", "status": "running"}


@app.post("/v1/notifications/sos", response_model=CreateResp)
async def create_sos(body: SOSNotificationRequest, db: AsyncSession = Depends(get_db)):
    """
    Create SOS notification - coordinates push/SMS/call from Notification service.
    """
    # Business metric: count new SOS notifications
    NOTIFICATION_SOS_CREATED_TOTAL.inc()
    try:
        resp = await manager.send_sos_notification(body)
        # Audit success
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=body.user_id if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.create sos_id={body.sos_id} notification_id={getattr(resp, 'notification_id', None)} status={getattr(resp, 'status', None)}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.create")
        return resp
    except Exception as e:
        # Audit failure
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=body.user_id if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.create_failed sos_id={body.sos_id} error={str(e)}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.create_failed")
        raise


@app.post("/v1/notifications/sos/sms", response_model=EmergencySMSResponse)
async def send_emergency_sms(body: EmergencySMSRequest, db: AsyncSession = Depends(get_db)):
    """
    SMS-only SOS sender for SOS service to proxy.
    """
    try:
        resp = await manager.send_emergency_sms(body)
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=body.user_id if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.sms_sent sos_id={body.sos_id} sms_id={resp.sms_id} recipient={resp.recipient} status={resp.status}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.sms_sent")
        return resp
    except Exception as e:
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=body.user_id if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.sms_failed sos_id={body.sos_id} error={str(e)}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.sms_failed")
        raise


@app.post("/v1/notifications/sos/call", response_model=EmergencyCallResponse)
async def send_emergency_call(body: EmergencyCallRequest, db: AsyncSession = Depends(get_db)):
    """
    Call-only SOS sender for SOS service to proxy.
    """
    try:
        resp = await manager.send_emergency_call(body)
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=body.user_id if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.call_initiated sos_id={body.sos_id} call_id={resp.call_id} status={resp.status}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.call_initiated")
        return resp
    except Exception as e:
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=body.user_id if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.call_failed sos_id={body.sos_id} error={str(e)}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.call_failed")
        raise


@app.get("/v1/notifications/{notification_id}", response_model=StatusResp)
async def get_status(notification_id: str, db: AsyncSession = Depends(get_db)):
    # Business metric: count status lookups
    NOTIFICATION_STATUS_CHECKS_TOTAL.inc()
    res = manager.get_status(notification_id)
    # Audit status check
    try:
        await write_audit(
            db=db,
            event_type="notification",
            user_id=None,
            event_id=None,
            message=f"notification.status_check notification_id={notification_id} status={getattr(res, 'status', None)}",
            commit=True,
        )
    except Exception:
        logger.exception("Failed to write audit for notification.status_check")
    return res


@app.post("/v1/test/sms", response_model=TestSMSResponse)
async def test_sms(body: TestSMSRequest, db: AsyncSession = Depends(get_db)):
    """
    Test endpoint to send an SMS to a phone number using Twilio.

    Phone number must be in E.164 format (e.g., +1234567890)
    """
    try:
        twilio = get_twilio_client()
        result = twilio.send_sms(to_phone=body.to_phone, message=body.message)

        resp = TestSMSResponse(
            status=result["status"],
            sid=result["sid"],
            to=result["to"],
            message=body.message,
            error=result.get("error"),
        )
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=None,
                event_id=None,
                message=f"notification.test_sms to={body.to_phone} status={resp.status} sid={resp.sid}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.test_sms")
        return resp
    except ValueError as e:
        # Twilio not configured
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=None,
                event_id=None,
                message=f"notification.test_sms_config_error to={body.to_phone} error={str(e)}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.test_sms_config_error")
        raise HTTPException(status_code=500, detail=f"Twilio configuration error: {str(e)}")
    except Exception as e:
        try:
            await write_audit(
                db=db,
                event_type="notification",
                user_id=None,
                event_id=None,
                message=f"notification.test_sms_failed to={body.to_phone} error={str(e)}",
                commit=True,
            )
        except Exception:
            logger.exception("Failed to write audit for notification.test_sms_failed")
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")


async def _claim_outbox_events(connection, limit: int) -> list[dict]:
    """
    Claim a batch of outbox events for processing.

    This is implemented with:
      - SELECT ... FOR UPDATE SKIP LOCKED to avoid multiple workers
      - then status updates to `processing`
    """
    claimed: list[dict] = []
    now = datetime.utcnow()

    async with connection.session_maker() as session:
        async with session.begin():
            result = await session.execute(
                text("""
                    SELECT
                      event_id,
                      event_type,
                      aggregate_id,
                      payload,
                      attempts,
                      max_attempts
                    FROM saferoute.outbox
                    WHERE status = 'pending'
                      AND available_at <= NOW()
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT :limit
                    """),
                {"limit": limit},
            )
            rows = result.fetchall()
            if not rows:
                return []

            for row in rows:
                # After this update, attempts will be incremented.
                await session.execute(
                    text("""
                        UPDATE saferoute.outbox
                        SET status = 'processing',
                            locked_at = :now,
                            attempts = attempts + 1,
                            updated_at = NOW()
                        WHERE event_id = :event_id
                        """),
                    {"now": now, "event_id": row.event_id},
                )

            # Return the rows with the updated attempt count.
            for row in rows:
                claimed.append(
                    {
                        "event_id": row.event_id,
                        "event_type": row.event_type,
                        "aggregate_id": row.aggregate_id,
                        "payload": row.payload,
                        "attempts_after_claim": row.attempts + 1,
                        "max_attempts": row.max_attempts,
                    }
                )

    return claimed


async def _handle_outbox_event(connection, event: dict) -> None:
    event_id = event["event_id"]
    event_type = event["event_type"]
    payload = event["payload"]
    attempts_after_claim: int = event["attempts_after_claim"]
    max_attempts: int = event["max_attempts"]

    now = datetime.utcnow()

    try:
        if event_type == "sos.emergency_call":
            em_id = uuid.UUID(str(payload["emergency_id"]))
            req = EmergencyCallRequest(
                emergency_id=em_id,
                phone_number=payload["phone_number"],
                user_location=payload["user_location"],
                call_reason=payload["call_reason"],
            )
            resp = await manager.send_emergency_call(req)

            # Best-effort notification worker audit.
            try:
                async with connection.session_maker() as session:
                    async with session.begin():
                        await write_audit(
                            db=session,
                            event_type="notification",
                            user_id=payload.get("user_id"),
                            event_id=em_id,
                            message=(
                                f"notification.worker_sos_call_sent "
                                f"emergency_id={em_id} call_id={resp.call_id} status={resp.status}"
                            ),
                            commit=False,
                        )
                        await session.execute(
                            text("""
                                UPDATE saferoute.outbox
                                SET status = 'done',
                                    processed_at = :now,
                                    last_error = NULL,
                                    updated_at = NOW()
                                WHERE event_id = :event_id
                                """),
                            {"now": now, "event_id": event_id},
                        )
            except Exception:
                logger.exception("Failed to write audit/done for outbox=%s", event_id)
            return

        # Unknown event types are marked done to prevent retry loops.
        async with connection.session_maker() as session:
            async with session.begin():
                await session.execute(
                    text("""
                        UPDATE saferoute.outbox
                        SET status = 'done',
                            processed_at = :now,
                            last_error = NULL,
                            updated_at = NOW()
                        WHERE event_id = :event_id
                        """),
                    {"now": now, "event_id": event_id},
                )
    except Exception as e:
        last_error = str(e)[:2000]

        # Retry with exponential backoff until max attempts.
        if attempts_after_claim >= max_attempts:
            new_status = "failed"
            available_at = None
        else:
            new_status = "pending"
            # backoff grows quickly, but cap at 60 seconds.
            backoff_seconds = min(60, 2 ** max(0, attempts_after_claim - 1))
            available_at = now + timedelta(seconds=backoff_seconds)

        async with connection.session_maker() as session:
            async with session.begin():
                if new_status == "failed":
                    await session.execute(
                        text("""
                            UPDATE saferoute.outbox
                            SET status = 'failed',
                                processed_at = :now,
                                last_error = :err,
                                updated_at = NOW()
                            WHERE event_id = :event_id
                            """),
                        {"now": now, "err": last_error, "event_id": event_id},
                    )
                else:
                    await session.execute(
                        text("""
                            UPDATE saferoute.outbox
                            SET status = 'pending',
                                available_at = :available_at,
                                last_error = :err,
                                updated_at = NOW()
                            WHERE event_id = :event_id
                            """),
                        {
                            "available_at": available_at,
                            "err": last_error,
                            "event_id": event_id,
                        },
                    )

        logger.exception(
            "Outbox event handling failed: event_id=%s event_type=%s status=%s err=%s",
            event_id,
            event_type,
            new_status,
            last_error,
        )


async def _outbox_worker_loop() -> None:
    connection = db_factory.get_connection(DatabaseType.POSTGRES)

    # Ensure tables exist for environments that didn't run migrations.
    try:
        async with connection.session_maker() as session:
            await ensure_outbox_tables(session)
    except Exception:
        logger.exception("Failed to ensure outbox tables; worker will still try")

    poll_seconds = OUTBOX_POLL_INTERVAL_SECONDS
    claim_limit = 10

    while True:
        try:
            events = await _claim_outbox_events(connection, limit=claim_limit)
            if not events:
                await asyncio.sleep(poll_seconds)
                continue

            for event in events:
                await _handle_outbox_event(connection, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox worker loop error")
            await asyncio.sleep(poll_seconds)


@app.on_event("startup")
async def _start_outbox_worker() -> None:
    if not OUTBOX_WORKER_ENABLED:
        return

    logger.info("Starting outbox worker (enabled via OUTBOX_WORKER_ENABLED=true)")
    # Store task on app state so shutdown can cancel it.
    app.state.outbox_worker_task = asyncio.create_task(_outbox_worker_loop())


@app.on_event("shutdown")
async def _shutdown_outbox_worker() -> None:
    task = getattr(app.state, "outbox_worker_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
