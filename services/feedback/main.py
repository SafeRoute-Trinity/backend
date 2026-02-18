# Run:
# uvicorn services.feedback.main:app --host 0.0.0.0 --port 20004 --reload
# Docs: http://127.0.0.1:20004/docs

import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import Depends, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from services.feedback.spam_validator import get_spam_validator_factory

# Initialize database connections
initialize_databases([DatabaseType.POSTGRES])

# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)

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


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    try:
        # Initialize spam validator factory
        validator_factory = get_spam_validator_factory()
        await validator_factory.initialize()
        logger.info("Spam validator initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize spam validator: {e}")
        # Continue startup even if spam validator fails


# ========= PROMETHEUS METRICS =========

SERVICE_NAME = "feedback"
registry = CollectorRegistry()

# Generic request counter
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
    registry=registry,
)

# Request latency
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
    registry=registry,
)

# Business-specific metrics
FEEDBACK_SUBMISSIONS_TOTAL = Counter(
    "feedback_submissions_total",
    "Total feedback submissions received",
    registry=registry,
)

FEEDBACK_VALIDATIONS_TOTAL = Counter(
    "feedback_validations_total",
    "Total feedback validation attempts",
    registry=registry,
)


FEEDBACK_STATUS_CHECKS_TOTAL = Counter(
    "feedback_status_checks_total",
    "Total feedback status lookups",
    registry=registry,
)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """
    Global metrics middleware to capture:
    - total requests
    - latency per endpoint
    """
    start = time.time()
    response = await call_next(request)

    path = request.url.path

    # Count request
    REQUEST_COUNT.labels(
        service=SERVICE_NAME,
        method=request.method,
        path=path,
        http_status=response.status_code,
    ).inc()

    # Measure latency
    REQUEST_LATENCY.labels(
        service=SERVICE_NAME,
        path=path,
    ).observe(time.time() - start)

    return response


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


# ========= MODELS =========


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


@app.post("/v1/feedback/submit", response_model=FeedbackSubmitResponse)
async def submit(body: FeedbackSubmitRequest, db: AsyncSession = Depends(get_db)):
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
    # Validate required UUIDs: feedback_id and user_id should be valid UUID strings
    parsed_user_id = _maybe_uuid(getattr(body, "user_id", None))
    parsed_feedback_id = _maybe_uuid(getattr(body, "feedback_id", None))
    if parsed_feedback_id is None:
        raise HTTPException(status_code=400, detail="feedback_id must be a valid UUID")
    if parsed_user_id is None:
        raise HTTPException(status_code=400, detail="user_id must be a valid UUID")

    resp = FeedbackSubmitResponse(
        feedback_id=body.feedback_id,
        status="received",
        ticket_number=ticket,
        created_at=now,
    )
    # Audit the submission (best-effort)
    try:
        await write_audit(
            db=db,
            event_type="feedback",
            user_id=parsed_user_id,
            event_id=parsed_feedback_id,
            message=f"feedback.submit feedback_id={body.feedback_id} ticket={ticket} type={body.type} severity={body.severity}",
            commit=True,
        )
    except Exception:
        logger.exception("Failed to write audit for feedback.submit")

    return resp


@app.post("/v1/feedback/validate", response_model=FeedbackValidateResponse)
async def validate(body: FeedbackValidateRequest, db: AsyncSession = Depends(get_db)):
    # Business metric
    FEEDBACK_VALIDATIONS_TOTAL.inc()

    # Validate user_id is a UUID (required for feedback validation traces)
    parsed_user_id = _maybe_uuid(getattr(body, "user_id", None))
    if parsed_user_id is None:
        raise HTTPException(status_code=400, detail="user_id must be a valid UUID")

    # Get spam validator and validate content
    try:
        validator_factory = get_spam_validator_factory()
        validator = validator_factory.get_validator()

        # Perform comprehensive spam validation
        validation_result = await validator.validate(
            content=body.content,
            recent_submissions_count=body.recent_submissions_count,
            max_urls_threshold=3,
            frequency_threshold=10,
        )

        resp = FeedbackValidateResponse(
            is_spam=validation_result.is_spam,
            confidence=validation_result.confidence,
            flags=validation_result.flags,
            allow_submission=validation_result.allow_submission,
            reason=validation_result.reason,
        )
    except Exception:
        logger.exception("Spam validation failed, falling back to basic check")
        # Fallback to basic frequency check
        is_spam = body.recent_submissions_count > 10
        resp = FeedbackValidateResponse(
            is_spam=is_spam,
            confidence=0.95 if is_spam else 0.9,
            flags=["high_frequency"] if is_spam else [],
            allow_submission=not is_spam,
            reason="OK" if not is_spam else "Too many submissions",
        )

    # Audit validation attempt
    try:
        await write_audit(
            db=db,
            event_type="feedback",
            user_id=parsed_user_id,
            event_id=None,
            message=f"feedback.validate user_id={body.user_id} is_spam={resp.is_spam} confidence={resp.confidence} allow_submission={resp.allow_submission} flags={','.join(resp.flags)}",
            commit=True,
        )
    except Exception:
        logger.exception("Failed to write audit for feedback.validate")

    return resp


@app.get("/v1/feedback/{feedback_id}/status", response_model=FeedbackStatusResponse)
async def status(feedback_id: str, db: AsyncSession = Depends(get_db)):
    # Business metric
    FEEDBACK_STATUS_CHECKS_TOTAL.inc()

    # TODO: when the Feedback schema finished, add the line below and get user_id
    # result = await db.execute(select(Feedback).where(Feedback.feedback_id == feedback_id))

    # Validate feedback_id is a UUID (feedback records should be UUIDs)
    parsed_feedback_id = _maybe_uuid(feedback_id)
    if parsed_feedback_id is None:
        raise HTTPException(status_code=400, detail="feedback_id must be a valid UUID")

    # TODO: when the Feedback schema finished, add the line below and get user_id
    # user_id = result.user_id
    user_id = None

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
    res = FeedbackStatusResponse(feedback_id=feedback_id, **fb)

    # Audit status lookup
    try:
        await write_audit(
            db=db,
            event_type="feedback",
            user_id=user_id,
            event_id=parsed_feedback_id,
            message=f"feedback.status_check feedback_id={feedback_id} ticket={fb.get('ticket_number')} status={fb.get('status')}",
            commit=True,
        )
    except Exception:
        logger.exception("Failed to write audit for feedback.status_check")

    return res


# ========= PROMETHEUS METRICS ENDPOINT =========


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
