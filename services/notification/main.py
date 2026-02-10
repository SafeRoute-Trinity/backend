# Run:
# uvicorn services.notification.main:app --host 0.0.0.0 --port 20001 --reload
# Docs: http://127.0.0.1:20001/docs

import os
import sys
import traceback

from dotenv import load_dotenv
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from manager import NotificationManager

from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.twilio_client import get_twilio_client
from models import (
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
async def create_sos(body: SOSNotificationRequest):
    """
    Create SOS notification - coordinates push/SMS/call from Notification service.
    """
    # Business metric: count new SOS notifications
    NOTIFICATION_SOS_CREATED_TOTAL.inc()
    return await manager.send_sos_notification(body)


@app.post("/v1/notifications/sos/sms", response_model=EmergencySMSResponse)
async def send_emergency_sms(body: EmergencySMSRequest):
    """
    SMS-only SOS sender for SOS service to proxy.
    """
    return await manager.send_emergency_sms(body)


@app.post("/v1/notifications/sos/call", response_model=EmergencyCallResponse)
async def send_emergency_call(body: EmergencyCallRequest):
    """
    Call-only SOS sender for SOS service to proxy.
    """
    return await manager.send_emergency_call(body)


@app.get("/v1/notifications/{notification_id}", response_model=StatusResp)
async def get_status(notification_id: str):
    # Business metric: count status lookups
    NOTIFICATION_STATUS_CHECKS_TOTAL.inc()
    return manager.get_status(notification_id)


@app.post("/v1/test/sms", response_model=TestSMSResponse)
async def test_sms(body: TestSMSRequest):
    """
    Test endpoint to send an SMS to a phone number using Twilio.

    Phone number must be in E.164 format (e.g., +1234567890)
    """
    try:
        twilio = get_twilio_client()
        result = twilio.send_sms(to_phone=body.to_phone, message=body.message)

        return TestSMSResponse(
            status=result["status"],
            sid=result["sid"],
            to=result["to"],
            message=body.message,
            error=result.get("error"),
        )
    except ValueError as e:
        # Twilio not configured
        raise HTTPException(status_code=500, detail=f"Twilio configuration error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")
