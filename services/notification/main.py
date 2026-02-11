# Run:
# uvicorn services.notification.main:app --host 0.0.0.0 --port 20001 --reload
# Docs: http://127.0.0.1:20001/docs

import os
import sys
import traceback

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


from manager import NotificationManager
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit
from libs.db import get_db
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.twilio_client import get_twilio_client

from manager import NotificationManager
from schemas import (
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
                user_id=_maybe_uuid(body.user_id) if hasattr(body, "user_id") else None,
                event_id=None,
                message=f"notification.create sos_id={body.sos_id} notification_id={getattr(resp,'notification_id', None)} status={getattr(resp,'status', None)}",
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
                user_id=_maybe_uuid(body.user_id) if hasattr(body, "user_id") else None,
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
                user_id=_maybe_uuid(body.user_id) if hasattr(body, "user_id") else None,
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
                user_id=_maybe_uuid(body.user_id) if hasattr(body, "user_id") else None,
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
                user_id=_maybe_uuid(body.user_id) if hasattr(body, "user_id") else None,
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
                user_id=_maybe_uuid(body.user_id) if hasattr(body, "user_id") else None,
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
            message=f"notification.status_check notification_id={notification_id} status={getattr(res,'status', None)}",
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
