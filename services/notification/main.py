# Run:
# uvicorn services.notification.main:app --host 0.0.0.0 --port 20001 --reload
# Docs: http://127.0.0.1:20001/docs

import time
import uuid
from datetime import datetime
from typing import Dict, Literal, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    CONTENT_TYPE_LATEST,
    generate_latest,
)
from pydantic import BaseModel

app = FastAPI(
    title="Notification Service",
    version="1.0.0",
    description="Create and check SOS notifications (SMS/Call).",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NOTIFICATIONS = {}

# ========= Metrics =========

SERVICE_NAME = "notification"
registry = CollectorRegistry()

# Generic per-request counter
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
    registry=registry,
)

# Latency histogram
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
    registry=registry,
)

# Business metrics
NOTIFICATION_SOS_CREATED_TOTAL = Counter(
    "notification_sos_created_total",
    "Total number of SOS notifications created",
    registry=registry,
)

NOTIFICATION_STATUS_CHECKS_TOTAL = Counter(
    "notification_status_checks_total",
    "Total number of notification status lookups",
    registry=registry,
)



@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """
    Track:
    - request count
    - latency per path
    """
    start = time.time()
    response = await call_next(request)

    path = request.url.path

    # Count requests
    REQUEST_COUNT.labels(
        service=SERVICE_NAME,
        method=request.method,
        path=path,
        http_status=response.status_code,
    ).inc()

    # Request time
    REQUEST_LATENCY.labels(
        service=SERVICE_NAME,
        path=path,
    ).observe(time.time() - start)

    return response


# ========= Models =========


class Location(BaseModel):
    lat: float
    lon: float
    accuracy_m: Optional[float] = None


class SOSContact(BaseModel):
    name: str
    phone: str


class SOSNotificationRequest(BaseModel):
    sos_id: str
    user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    call_number: str
    message_template: str
    variables: Dict[str, str]


class CreateResp(BaseModel):
    notification_id: str
    status: Literal["queued", "sending", "delivered", "failed"]


class StatusResult(BaseModel):
    sms_status: Literal["queued", "sending", "delivered", "failed", "not_triggered"]
    call_status: Literal["queued", "calling", "answered", "failed", "not_triggered"]


class StatusResp(BaseModel):
    notification_id: str
    sos_id: str
    status: Literal["queued", "sending", "delivered", "failed", "partial"]
    results: StatusResult
    created_at: datetime
    updated_at: datetime


# ========= Routes =========


@app.get("/")
async def root():
    return {"service": "notification", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification"}


@app.post("/v1/notifications/sos", response_model=CreateResp)
async def create_sos(body: SOSNotificationRequest):
    # Business metric: count new SOS notifications
    NOTIFICATION_SOS_CREATED_TOTAL.inc()

    nid = f"ntf_{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    NOTIFICATIONS[nid] = {
        "notification_id": nid,
        "sos_id": body.sos_id,
        "status": "queued",
        "results": {"sms_status": "queued", "call_status": "not_triggered"},
        "created_at": now,
        "updated_at": now,
    }
    return CreateResp(notification_id=nid, status="queued")


@app.get("/v1/notifications/{notification_id}", response_model=StatusResp)
async def get_status(notification_id: str):
    # Business metric: count status lookups
    NOTIFICATION_STATUS_CHECKS_TOTAL.inc()

    ntf = NOTIFICATIONS.get(notification_id)
    now = datetime.utcnow()
    if not ntf:
        ntf = {
            "notification_id": notification_id,
            "sos_id": "SOS-demo",
            "status": "delivered",
            "results": {"sms_status": "delivered", "call_status": "not_triggered"},
            "created_at": now,
            "updated_at": now,
        }
    return StatusResp(**ntf)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
