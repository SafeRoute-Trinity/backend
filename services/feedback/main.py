# Run:
# uvicorn services.feedback.main:app --host 0.0.0.0 --port 20004 --reload
# Docs: http://127.0.0.1:20004/docs

import os
import sys
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, HttpUrl

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)

# Create service configuration
service_config = ServiceAppConfig(
    title="Feedback Service",
    description="Submit/validate feedback and check status.",
    service_name="feedback",
    cors_config=CORSMiddlewareConfig(),
)

# Create factory and build app
factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

# Add business-specific metrics
FEEDBACK_SUBMISSIONS_TOTAL = factory.add_business_metric(
    "feedback_submissions_total",
    "Total feedback submissions received",
)

FEEDBACK_VALIDATIONS_TOTAL = factory.add_business_metric(
    "feedback_validations_total",
    "Total feedback validation attempts",
)

FEEDBACK_STATUS_CHECKS_TOTAL = factory.add_business_metric(
    "feedback_status_checks_total",
    "Total feedback status lookups",
)

FEEDBACK = {}


# ========= MODELS =========


class FeedbackLocation(BaseModel):
    lat: float
    lon: float


class FeedbackSubmitRequest(BaseModel):
    feedback_id: str
    user_id: str
    route_id: Optional[str] = None
    session_id: Optional[str] = None
    type: Literal["safety_issue", "route_quality", "other"]
    location: Optional[FeedbackLocation] = None
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    attachments: Optional[List[HttpUrl]] = None
    timestamp: datetime


class FeedbackSubmitResponse(BaseModel):
    feedback_id: str
    status: Literal["received"]
    ticket_number: str
    created_at: datetime


class FeedbackValidateRequest(BaseModel):
    user_id: str
    content: str
    recent_submissions_count: int


class FeedbackValidateResponse(BaseModel):
    is_spam: bool
    confidence: float
    flags: List[str]
    allow_submission: bool
    reason: str


class FeedbackStatusResponse(BaseModel):
    feedback_id: str
    ticket_number: str
    status: Literal["under_review", "resolved", "rejected", "received"]
    type: Literal["safety_issue", "route_quality", "other"]
    severity: Literal["low", "medium", "high", "critical"]
    created_at: datetime
    updated_at: datetime


# ========= ROUTES =========


@app.get("/")
async def root():
    return {"service": "feedback", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "feedback"}


@app.post("/v1/feedback/submit", response_model=FeedbackSubmitResponse)
async def submit(body: FeedbackSubmitRequest):
    # Business metric
    FEEDBACK_SUBMISSIONS_TOTAL.inc()

    now = datetime.utcnow()
    ticket = f"TKT-{now.year}-{uuid.uuid4().hex[:6]}"
    FEEDBACK[body.feedback_id] = {
        "ticket_number": ticket,
        "status": "under_review",
        "type": body.type,
        "severity": body.severity,
        "created_at": now,
        "updated_at": now,
    }
    return FeedbackSubmitResponse(
        feedback_id=body.feedback_id,
        status="received",
        ticket_number=ticket,
        created_at=now,
    )


@app.post("/v1/feedback/validate", response_model=FeedbackValidateResponse)
async def validate(body: FeedbackValidateRequest):
    # Business metric
    FEEDBACK_VALIDATIONS_TOTAL.inc()

    is_spam = body.recent_submissions_count > 10
    return FeedbackValidateResponse(
        is_spam=is_spam,
        confidence=0.95 if is_spam else 0.9,
        flags=["high_frequency"] if is_spam else [],
        allow_submission=not is_spam,
        reason="OK" if not is_spam else "Too many submissions",
    )


@app.get("/v1/feedback/{feedback_id}/status", response_model=FeedbackStatusResponse)
async def status(feedback_id: str):
    # Business metric
    FEEDBACK_STATUS_CHECKS_TOTAL.inc()

    fb = FEEDBACK.get(feedback_id)
    now = datetime.utcnow()
    if not fb:
        fb = {
            "ticket_number": f"TKT-{now.year}-DEMO",
            "status": "under_review",
            "type": "safety_issue",
            "severity": "high",
            "created_at": now,
            "updated_at": now,
        }
    return FeedbackStatusResponse(feedback_id=feedback_id, **fb)
