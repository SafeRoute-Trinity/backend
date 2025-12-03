# Run:
# uvicorn services.sos.main:app --host 0.0.0.0 --port 20006 --reload
# Docs: http://127.0.0.1:20006/docs

import os
import sys
import time
import uuid
from datetime import datetime
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

# Load environment variables from .env file
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.rabbitmq_client import get_rabbitmq_client
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

# ========= Metrics =========

SERVICE_NAME = "sos"
registry = CollectorRegistry()

# Generic per-request counter (shared schema with other services)
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
    registry=registry,
)

# Latency histogram per path
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
    registry=registry,
)

# Business metrics: SOS call & SMS usage
SOS_CALLS_TOTAL = Counter(
    "sos_calls_total",
    "Total SOS emergency calls initiated",
    registry=registry,
)

SOS_SMS_TOTAL = Counter(
    "sos_sms_total",
    "Total SOS emergency SMS sent",
    registry=registry,
)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """
    Middleware to track:
    - request count
    - latency per path
    for every HTTP request handled by this service.
    """
    start = time.time()
    response = await call_next(request)

    path = request.url.path

    # Increment per-request counter
    REQUEST_COUNT.labels(
        service=SERVICE_NAME,
        method=request.method,
        path=path,
        http_status=response.status_code,
    ).inc()

    # Record latency
    REQUEST_LATENCY.labels(
        service=SERVICE_NAME,
        path=path,
    ).observe(time.time() - start)

    return response


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

    # Business metric: count SOS calls
    SOS_CALLS_TOTAL.inc()

    return EmergencyCallResponse(status="initiated", call_id=cid, timestamp=now)


async def publish_sms_to_queue(body: EmergencySMSRequest) -> bool:
    """
    Publish SMS notification to RabbitMQ queue.

    Returns:
        True if message was published successfully, False otherwise
    """
    try:
        rabbitmq = get_rabbitmq_client()

        # Prepare message payload
        message_payload = {
            "type": "sms",
            "sos_id": body.sos_id,
            "user_id": body.user_id,
            "location": body.location.dict() if body.location else None,
            "emergency_contact": body.emergency_contact.dict(),
            "message_template": body.message_template,
            "variables": body.variables,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Try to connect and publish
        if not rabbitmq.connection or rabbitmq.connection.is_closed:
            if not rabbitmq.connect():
                return False

        # Publish to notification queue
        queue_name = os.getenv("RABBITMQ_NOTIFICATION_QUEUE", "notifications")
        success = rabbitmq.publish(queue_name=queue_name, message=message_payload)

        if success:
            print(f"✓ Published SMS notification to RabbitMQ queue '{queue_name}'")
        else:
            print("✗ Failed to publish SMS notification to RabbitMQ")

        return success
    except Exception as e:
        print(f"✗ Error publishing to RabbitMQ: {e}")
        return False


def send_sms_directly(body: EmergencySMSRequest, formatted_message: str) -> dict:
    """
    Send SMS directly via Twilio (fallback when RabbitMQ is unavailable).

    Returns:
        dict with status, sms_id, and error info
    """
    try:
        twilio = get_twilio_client()
        result = twilio.send_sms(
            to_phone=body.emergency_contact.phone, message=formatted_message
        )

        if result["status"] == "sent":
            return {
                "status": "sent",
                "sms_id": result["sid"],
                "error": None,
            }
        else:
            return {
                "status": "failed",
                "sms_id": f"SMS-{uuid.uuid4().hex[:6]}",
                "error": result.get("error"),
            }

    except ValueError as e:
        # Twilio not configured
        print(f"Twilio configuration error: {e}")
        return {
            "status": "failed",
            "sms_id": f"SMS-{uuid.uuid4().hex[:6]}",
            "error": f"Twilio configuration error: {str(e)}",
        }
    except Exception as e:
        print(f"Error sending SMS: {e}")
        return {
            "status": "failed",
            "sms_id": f"SMS-{uuid.uuid4().hex[:6]}",
            "error": str(e),
        }


@app.post("/v1/emergency/sms", response_model=EmergencySMSResponse)
async def sms(body: EmergencySMSRequest):
    """
    Send emergency SMS with rich details (templates, variables, location).
    First tries to publish to RabbitMQ queue, falls back to direct Twilio if queue unavailable.
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

    # Try RabbitMQ first, fallback to direct Twilio
    try:
        queue_success = await publish_sms_to_queue(body)

        if queue_success:
            # Message queued successfully - return queued status
            sid = f"SMS-QUEUED-{uuid.uuid4().hex[:6]}"
            sms_status = "queued"
            status = "sent"  # Return "sent" to indicate request was accepted
            print("✓ SMS notification queued in RabbitMQ")
        else:
            # Fallback to direct Twilio send
            print("⚠ RabbitMQ unavailable, falling back to direct Twilio send")
            result = send_sms_directly(body, message)
            sid = result["sms_id"]
            sms_status = result["status"]
            status = result["status"]
            if result.get("error"):
                print(f"SMS send failed: {result['error']}")

    except Exception as e:
        print(f"Error in SMS handler: {e}")
        # Fallback to direct send on any error
        result = send_sms_directly(body, message)
        sid = result["sms_id"]
        sms_status = result["status"]
        status = result["status"]

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

    # Business metric: count SOS SMS
    SOS_SMS_TOTAL.inc()

    return EmergencySMSResponse(
        status=status,
        sms_id=sid,
        timestamp=now,
        message_sent=message,
        recipient=body.emergency_contact.phone,
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


@app.get("/metrics")
async def metrics():
    """
    Expose Prometheus metrics for this SOS service.
    """
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


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
        raise HTTPException(
            status_code=500, detail=f"Twilio configuration error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")
