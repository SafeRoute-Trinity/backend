# Run:
# uvicorn services.feedback.main:app --host 0.0.0.0 --port 20004 --reload
# Docs: http://127.0.0.1:20004/docs

import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import List, Optional, TypeVar, Generic, Dict, Any

from fastapi import Depends, HTTPException, Request, Response, Query
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
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
from services.feedback.feedback_factory import get_feedback_factory
from services.feedback.spam_validator import get_spam_validator_factory
from services.feedback.types import FeedbackType, SeverityType, Status

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

    try:
        # Initialize feedback factory
        feedback_factory = get_feedback_factory()
        feedback_factory.initialize()
        logger.info("Feedback factory initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize feedback factory: {e}")
        # Continue startup even if feedback factory fails


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


class FeedbackLocation(BaseModel):
    lat: float
    lon: float


class FeedbackSubmitRequest(BaseModel):
    user_id: str
    route_id: Optional[str] = None
    type: Optional[FeedbackType] = None
    severity: Optional[SeverityType] = None
    location: Optional[dict] = None
    description: Optional[str] = None
    attachments: Optional[list] = None


class FeedbackSubmitResponse(BaseModel):
    feedback_id: uuid.UUID
    status: Status
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
    feedback_id: uuid.UUID
    ticket_number: str
    status: Status
    type: Optional[FeedbackType] = None
    severity: Optional[SeverityType] = None
    created_at: datetime
    updated_at: datetime


class PaginationMeta(BaseModel):
    """Metadata for paginated list responses."""

    page: int = Field(..., ge=1, description="Current page (1-based)")
    page_size: int = Field(..., ge=1, le=100, description="Items per page")
    total: int = Field(..., ge=0, description="Total number of items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")


T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Paginated list of feedback with filters."""

    data: List[T]
    filters: Dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationMeta


# ========= ROUTES =========


@app.get("/")
async def root():
    return {"service": "feedback", "status": "running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "feedback"}


@app.post("/v1/feedback/submit", response_model=FeedbackSubmitResponse)
async def submit(body: FeedbackSubmitRequest, db: AsyncSession = Depends(get_db)):
    # Business metric
    FEEDBACK_SUBMISSIONS_TOTAL.inc()

    # Extract and validate user_id (handles Auth0 format: "auth0|user_id" or just "user_id")
    user_id_str = getattr(body, "user_id", None)
    if not user_id_str:
        raise HTTPException(status_code=400, detail="user_id is required")

    # Remove Auth0 prefix if present (format: "auth0|user_id" or "auth0_user_id")
    if user_id_str.startswith("auth0|"):
        user_id_str = user_id_str.split("|")[-1]
    elif user_id_str.startswith("auth0_"):
        user_id_str = user_id_str.replace("auth0_", "", 1)

    # For audit logging, try to parse as UUID if possible
    parsed_user_id = _maybe_uuid(user_id_str)

    # Extract lat/lon from location dict if provided
    lat = None
    lon = None
    location_dict = None
    if body.location:
        if isinstance(body.location, dict):
            lat = body.location.get("lat")
            lon = body.location.get("lon")
            location_dict = body.location
        elif hasattr(body.location, "lat") and hasattr(body.location, "lon"):
            lat = body.location.lat
            lon = body.location.lon
            location_dict = {"lat": lat, "lon": lon}

    # Generate feedback_id and timestamp
    feedback_id = uuid.uuid4()
    now = datetime.utcnow()

    # Generate ticket number as string (format: TKT-YYYY-XXXXXX)
    ticket_number = f"TKT-{now.year}-{uuid.uuid4().hex[:6]}"

    # Get feedback factory and create feedback record in database
    feedback_factory = get_feedback_factory()
    try:
        await feedback_factory.create_feedback(
            db=db,
            feedback_id=feedback_id,
            user_id=user_id_str,
            ticket_number=ticket_number,
            route_id=body.route_id,
            lat=lat,
            lon=lon,
            type=body.type,
            severity=body.severity,
            description=body.description,
            location=location_dict,
            attachments=body.attachments,
            status=Status.RECEIVED,
            created_at=now,
        )

        # Commit the transaction
        await db.commit()

        logger.info(
            f"Feedback created successfully: feedback_id={feedback_id} ticket={ticket_number}"
        )
    except Exception as e:
        await db.rollback()
        logger.exception(f"Failed to create feedback in database: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to submit feedback: {str(e)}")

    # Also store in in-memory cache for backward compatibility
    FEEDBACK[str(feedback_id)] = {
        "ticket_number": ticket_number,
        "status": Status.RECEIVED.value,
        "type": body.type.value if body.type else None,
        "severity": body.severity.value if body.severity else None,
        "created_at": now,
        "updated_at": now,
        "user_id": user_id_str,
    }

    # Add optional fields if provided
    if body.route_id is not None:
        FEEDBACK[str(feedback_id)]["route_id"] = body.route_id
    if location_dict is not None:
        FEEDBACK[str(feedback_id)]["location"] = location_dict
    if body.description is not None:
        FEEDBACK[str(feedback_id)]["description"] = body.description
    if body.attachments is not None:
        FEEDBACK[str(feedback_id)]["attachments"] = [str(att) for att in body.attachments]

    resp = FeedbackSubmitResponse(
        feedback_id=feedback_id,
        status=Status.RECEIVED,
        ticket_number=ticket_number,
        created_at=now,
    )

    # Audit the submission (best-effort)
    try:
        await write_audit(
            db=db,
            event_type="feedback",
            user_id=parsed_user_id,
            event_id=feedback_id,
            message=f"feedback.submit feedback_id={feedback_id} ticket={ticket_number} type={body.type} severity={body.severity} route_id={body.route_id}",
            commit=True,
        )
    except Exception:
        logger.exception("Failed to write audit for feedback.submit")

    return resp


@app.get("/v1/feedback", response_model=PaginatedResponse[FeedbackStatusResponse])
async def list_feedback(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    status: Optional[Status] = Query(
        None, description="Filter by feedback status (received, resolved, rejected)"
    ),
    type: Optional[FeedbackType] = Query(
        None,
        description="Filter by feedback type (safety_issue, route_quality, others)",
    ),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
    db: AsyncSession = Depends(get_db),
):
    """
    Retrieve a paginated list of historical feedbacks with optional filtering.
    """
    skip = (page - 1) * page_size

    feedback_factory = get_feedback_factory()

    total_count, feedbacks = await feedback_factory.get_feedbacks(
        db=db,
        user_id=user_id,
        status=status,
        feedback_type=type,
        skip=skip,
        limit=page_size,
    )

    data = []
    for row in feedbacks:
        api_type = row.type if row.type else None
        if api_type == "others":
            api_type = "other"

        data.append(
            FeedbackStatusResponse(
                feedback_id=row.feedback_id,
                ticket_number=row.ticket_number or f"TKT-{datetime.utcnow().year}-DB",
                status=row.status,
                type=api_type,
                severity=row.severity,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )

    total_pages = max(0, (total_count + page_size - 1) // page_size) if page_size > 0 else 0

    applied_filters = {
        "status": status.value if status else "",
        "type": type.value if type else "",
        "user_id": user_id if user_id else "",
    }

    pagination_meta = PaginationMeta(
        page=page, page_size=page_size, total=total_count, total_pages=total_pages
    )

    return PaginatedResponse(data=data, filters=applied_filters, pagination=pagination_meta)


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
async def status(feedback_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    FEEDBACK_STATUS_CHECKS_TOTAL.inc()

    # === DB-backed status lookup ===
    row = (
        (
            await db.execute(
                text("""
                SELECT
                  feedback_id,
                  user_id,
                  ticket_number,
                  status,
                  type AS feedback_type,
                  severity,
                  created_at,
                  updated_at
                FROM saferoute.feedback
                WHERE feedback_id = :fid
                """),
                {"fid": str(feedback_id)},
            )
        )
        .mappings()
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail="feedback not found")

    api_type = row["feedback_type"]
    if api_type == "others":
        api_type = "other"

    # Extract user_id from the feedback record and parse it for audit logging
    user_id_str = row.get("user_id")
    parsed_user_id = _maybe_uuid(user_id_str) if user_id_str else None

    # Extract feedback_id as UUID from database row (already a UUID object)
    feedback_id_uuid = row["feedback_id"]
    if not isinstance(feedback_id_uuid, uuid.UUID):
        # Fallback: use the validated UUID from path parameter
        feedback_id_uuid = feedback_id

    res = FeedbackStatusResponse(
        feedback_id=str(row["feedback_id"]),
        ticket_number=(
            str(row["ticket_number"])
            if row["ticket_number"] is not None
            else f"TKT-{datetime.utcnow().year}-DB"
        ),
        status=row["status"],
        type=api_type,
        severity=row["severity"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

    try:
        await write_audit(
            db=db,
            event_type="feedback",
            user_id=parsed_user_id,
            event_id=feedback_id_uuid,
            message=f"feedback.status_check feedback_id={feedback_id} ticket={row.get('ticket_number')} status={row.get('status')}",
            commit=True,
        )
    except Exception:
        logger.exception("Failed to write audit for feedback.status_check")

    return res


# ========= PROMETHEUS METRICS ENDPOINT =========


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
