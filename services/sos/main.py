# Run:
# uvicorn services.sos.main:app --host 0.0.0.0 --port 20006 --reload
# Docs: http://127.0.0.1:20006/docs

import time
import uuid
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Path, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Histogram,
                               generate_latest)
from pydantic import BaseModel, HttpUrl

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

# Generic per-request counter (shared schema with other services)
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
)

# Latency histogram per path
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
)

# Business metrics: SOS call & SMS usage
SOS_CALLS_TOTAL = Counter(
    "sos_calls_total",
    "Total SOS emergency calls initiated",
)

SOS_SMS_TOTAL = Counter(
    "sos_sms_total",
    "Total SOS emergency SMS sent",
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


class EmergencySMSRequest(BaseModel):
    sos_id: str
    recipient_phone: str
    message: str
    location_url: HttpUrl


class EmergencySMSResponse(BaseModel):
    status: Literal["sent", "failed"]
    sms_id: str
    timestamp: datetime


class EmergencyStatusResponse(BaseModel):
    sos_id: str
    call_status: Literal["initiated", "connected", "failed", "not_triggered"]
    sms_status: Literal["sent", "failed", "not_sent"]
    last_update: datetime


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


@app.post("/v1/emergency/sms", response_model=EmergencySMSResponse)
async def sms(body: EmergencySMSRequest):
    sid = f"SMS-{uuid.uuid4().hex[:6]}"
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
    s["sms_status"] = "sent"
    s["last_update"] = now

    # Business metric: count SOS SMS
    SOS_SMS_TOTAL.inc()

    return EmergencySMSResponse(status="sent", sms_id=sid, timestamp=now)


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
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
