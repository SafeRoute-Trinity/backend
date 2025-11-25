# Run:
# uvicorn services.feedback.main:app --host 0.0.0.0 --port 20004 --reload
# Docs: http://127.0.0.1:20004/docs

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

app = FastAPI(
    title="Feedback Service",
    version="1.0.0",
    description="Submit/validate feedback and check status.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FEEDBACK = {}


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


@app.get("/")
async def root():
    return {"service": "feedback", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "feedback"}


@app.get("/v1/feedback/metrics")
async def metrics():
    return {"service": "feedback", "status": "running"}


@app.post("/v1/feedback/submit", response_model=FeedbackSubmitResponse)
async def submit(body: FeedbackSubmitRequest):
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
