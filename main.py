#uvicorn main:app --host 0.0.0.0 --port 5000 --reload
#uvicorn main:app --host 0.0.0.0 --port 12345 --reload

#http://127.0.0.1:12345/docs

from fastapi import FastAPI, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import List, Optional, Dict, Literal
from datetime import datetime
import uuid

app = FastAPI(
    title="SafeRoute API (Mock)",
    description=(
        "Mock implementation of SafeRoute backend APIs based on the architecture spec. "
        "Endpoints return example / in-memory stub data for interactive testing via /docs."
    ),
    version="1.0.0",
)

# CORS（方便本地和前端调试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境记得收紧
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= In-memory mock storage =========
users: Dict[str, dict] = {}
trusted_contacts: Dict[str, List[dict]] = {}
notifications: Dict[str, dict] = {}
routes: Dict[str, dict] = {}
nav_sessions: Dict[str, dict] = {}
feedback_store: Dict[str, dict] = {}
audit_logs: List[dict] = []
data_batches: Dict[str, dict] = {}
emergency_status: Dict[str, dict] = {}

# ========= 共用模型 =========

class Point(BaseModel):
    lat: float
    lon: float



# ========= 2. Notification =========

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

class NotificationCreateResponse(BaseModel):
    notification_id: str
    status: Literal["queued", "sending", "delivered", "failed"]

class NotificationStatusResult(BaseModel):
    sms_status: Literal["queued", "sending", "delivered", "failed", "not_triggered"]
    call_status: Literal["queued", "calling", "answered", "failed", "not_triggered"]

class NotificationStatusResponse(BaseModel):
    notification_id: str
    sos_id: str
    status: Literal["queued", "sending", "delivered", "failed", "partial"]
    results: NotificationStatusResult
    created_at: datetime
    updated_at: datetime

# ========= 3. Routing & Navigation =========

class RoutePreferences(BaseModel):
    optimize_for: Literal["safety", "time", "distance", "balanced"]
    avoid: Optional[List[str]] = None
    transport_mode: Literal["walking", "cycling", "driving", "public_transit"]

class RouteCalculateRequest(BaseModel):
    origin: Point
    destination: Point
    user_id: str
    preferences: RoutePreferences
    time_of_day: Optional[datetime] = None

class SafetySegment(BaseModel):
    segment_id: str
    score: float
    risk_level: Optional[str] = None

class Waypoint(BaseModel):
    lat: float
    lon: float
    instruction: Optional[str] = None

class RouteOption(BaseModel):
    route_index: int
    is_primary: bool
    geometry: str
    distance_m: int
    duration_s: int
    safety_score: float
    waypoints: List[Waypoint] = []
    safety_segments: List[SafetySegment] = []

class RouteCalculateResponse(BaseModel):
    route_id: str
    routes: List[RouteOption]
    alternatives_count: int
    calculated_at: datetime

class RecalculateRequest(BaseModel):
    route_id: str
    current_location: Point
    reason: Literal["off_track", "road_closure", "user_request", "safety_alert"]

class RecalculateResponse(RouteCalculateResponse):
    previous_route_id: str
    recalculated: bool

class NavigationStartRequest(BaseModel):
    route_id: str
    user_id: str
    estimated_arrival: datetime

class NavigationStartResponse(BaseModel):
    session_id: str
    status: Literal["active"]
    tracking_enabled: bool
    next_checkpoint_m: int
    started_at: datetime

class NavigationUpdateRequest(BaseModel):
    current_location: Point
    bearing: Optional[float] = None

class NavigationUpdateResponse(BaseModel):
    status: Literal["on_track", "off_track"]
    next_instruction: Optional[str] = None
    distance_to_destination_m: Optional[int] = None
    eta: Optional[datetime] = None
    safety_alert: Optional[str] = None
    current_segment_score: Optional[float] = None

class NavigationEndRequest(BaseModel):
    completion_status: Literal["completed", "cancelled", "interrupted"]
    actual_arrival_time: datetime

class SessionSummary(BaseModel):
    total_distance_m: int
    total_duration_s: int
    route_deviations: int
    avg_safety_score: float
    started_at: datetime
    ended_at: datetime

class NavigationEndResponse(BaseModel):
    session_id: str
    status: Literal["completed"]
    session_summary: SessionSummary

# ========= 4. SafetyScore =========

class SafetySegmentInput(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float

class ScoreRouteRequest(BaseModel):
    route_geometry: str
    segments: List[SafetySegmentInput]
    time_of_day: datetime
    weather_conditions: Optional[Literal["clear", "rain", "fog"]] = None

class RiskFactor(BaseModel):
    type: str
    severity: str

class SafetySegmentScore(BaseModel):
    segment_id: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    score: float
    risk_factors: List[RiskFactor] = []

class SafetyAlert(BaseModel):
    type: str
    location: Point
    severity: str
    message: str

class ScoreRouteResponse(BaseModel):
    overall_score: float
    scoring_breakdown: Dict[str, float]
    segments: List[SafetySegmentScore]
    alerts: List[SafetyAlert]
    calculated_at: datetime

class SafetyFactorsRequest(BaseModel):
    lat: float
    lon: float
    radius_m: int = 50

class SafetyFactorsResponse(BaseModel):
    location: Point
    radius_m: int
    factors: Dict[str, object]
    composite_score: float
    queried_at: datetime

class SafetyWeights(BaseModel):
    cctv_coverage: float
    street_lighting: float
    business_activity: float
    crime_rate: float
    pedestrian_traffic: float

class SafetyWeightsRequest(BaseModel):
    user_id: str
    weights: SafetyWeights

class SafetyWeightsResponse(BaseModel):
    status: Literal["updated"]
    user_id: str
    weights: SafetyWeights
    weights_sum: float
    updated_at: datetime

# ========= 5. Feedback =========

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
    spam_check: Literal["passed", "flagged"]
    estimated_review_time_h: int
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
    admin_response: Optional[str] = None
    resolution: Optional[str] = None

# ========= 6. Data Management & Audit =========

class AuditLogRequest(BaseModel):
    event_id: str
    event_type: Literal["user_action", "system_event", "security_event", "data_access"]
    user_id: Optional[str] = None
    component: str
    action: str
    resource: Optional[str] = None
    metadata: Dict[str, object] = {}
    ip_address: Optional[str] = None
    timestamp: datetime
    severity: Literal["info", "warning", "error", "critical"]

class AuditLogResponse(BaseModel):
    log_id: str
    status: Literal["recorded"]
    retention_until: datetime
    created_at: datetime

class DataCollectRequest(BaseModel):
    source: Literal["dcc_cameras", "tfi_transit", "crime_stats", "postGIS"]
    data_type: str
    batch_id: str
    records: List[Dict[str, object]]
    collected_at: datetime

class DataCollectResponse(BaseModel):
    batch_id: str
    status: Literal["processing"]
    records_received: int
    estimated_processing_time_s: int
    queued_at: datetime

class DataSanitizeRequest(BaseModel):
    batch_id: str
    rules: List[str]

class DataSanitizeSummary(BaseModel):
    null_values_removed: int
    identifiers_masked: int
    outliers_dropped: int

class DataSanitizeResponse(BaseModel):
    batch_id: str
    status: Literal["completed"]
    total_records: int
    processed_records: int
    errors: int
    start_time: datetime
    end_time: datetime
    applied_rules: List[str]
    summary: DataSanitizeSummary

class AuditQueryRequest(BaseModel):
    user_id: Optional[str] = None
    event_type: Optional[str] = None
    component: Optional[str] = None
    date_from: datetime
    date_to: datetime
    limit: int = 100
    offset: int = 0

class AuditLogEntry(BaseModel):
    log_id: str
    timestamp: datetime
    user_id: Optional[str] = None
    event_type: str
    component: str
    action: str
    details: str

class AuditQueryResponse(BaseModel):
    total_results: int
    limit: int
    offset: int
    logs: List[AuditLogEntry]

# ========= 7. External Emergency (SOS) =========

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

# ========= Root / Health =========

@app.get("/")
async def root():
    return {"message": "SafeRoute Mock API is running"}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "SafeRoute Mock API"}



# ========= Notification =========

@app.post("/v1/notifications/sos",
          response_model=NotificationCreateResponse,
          tags=["Notification"])
async def create_sos_notification(payload: SOSNotificationRequest):
    ntf_id = f"ntf_{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    notifications[ntf_id] = {
        "notification_id": ntf_id,
        "sos_id": payload.sos_id,
        "status": "queued",
        "results": {
            "sms_status": "queued",
            "call_status": "not_triggered",
        },
        "created_at": now,
        "updated_at": now,
    }
    return NotificationCreateResponse(notification_id=ntf_id, status="queued")

@app.get("/v1/notifications/{notification_id}",
         response_model=NotificationStatusResponse,
         tags=["Notification"])
async def get_notification_status(notification_id: str):
    ntf = notifications.get(notification_id)
    now = datetime.utcnow()
    if not ntf:
        ntf = {
            "notification_id": notification_id,
            "sos_id": "SOS-demo",
            "status": "delivered",
            "results": {
                "sms_status": "delivered",
                "call_status": "not_triggered",
            },
            "created_at": now,
            "updated_at": now,
        }
    return NotificationStatusResponse(
        notification_id=ntf["notification_id"],
        sos_id=ntf["sos_id"],
        status=ntf["status"],
        results=NotificationStatusResult(**ntf["results"]),
        created_at=ntf["created_at"],
        updated_at=ntf["updated_at"],
    )

# ========= Routing & Navigation =========

@app.post("/v1/routes/calculate",
          response_model=RouteCalculateResponse,
          tags=["Routing"])
async def calculate_route(payload: RouteCalculateRequest):
    route_id = f"rt_{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    primary = RouteOption(
        route_index=0,
        is_primary=True,
        geometry="encoded_polyline_demo",
        distance_m=2450,
        duration_s=1800,
        safety_score=87.5,
        waypoints=[
            Waypoint(lat=payload.origin.lat, lon=payload.origin.lon,
                     instruction="Start here"),
            Waypoint(lat=payload.destination.lat, lon=payload.destination.lon,
                     instruction="Arrive here"),
        ],
        safety_segments=[
            SafetySegment(segment_id="seg_001", score=92, risk_level="low")
        ],
    )
    routes[route_id] = {
        "route_id": route_id,
        "routes": [primary],
        "alternatives_count": 1,
        "calculated_at": now,
    }
    return RouteCalculateResponse(
        route_id=route_id,
        routes=[primary],
        alternatives_count=1,
        calculated_at=now,
    )

@app.post("/v1/routes/{route_id}/recalculate",
          response_model=RecalculateResponse,
          tags=["Routing"])
async def recalculate_route(route_id: str, payload: RecalculateRequest):
    now = datetime.utcnow()
    new_route_id = f"rt_{uuid.uuid4().hex[:6]}"
    option = RouteOption(
        route_index=0,
        is_primary=True,
        geometry="recalculated_polyline_demo",
        distance_m=1850,
        duration_s=1400,
        safety_score=89.2,
        waypoints=[],
        safety_segments=[],
    )
    return RecalculateResponse(
        previous_route_id=route_id,
        recalculated=True,
        route_id=new_route_id,
        routes=[option],
        alternatives_count=1,
        calculated_at=now,
    )

@app.post("/v1/navigation/start",
          response_model=NavigationStartResponse,
          tags=["Routing"])
async def start_navigation(payload: NavigationStartRequest):
    session_id = f"nav_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    nav_sessions[session_id] = {
        "route_id": payload.route_id,
        "user_id": payload.user_id,
        "started_at": now,
    }
    return NavigationStartResponse(
        session_id=session_id,
        status="active",
        tracking_enabled=True,
        next_checkpoint_m=500,
        started_at=now,
    )

@app.put("/v1/navigation/{session_id}/update",
         response_model=NavigationUpdateResponse,
         tags=["Routing"])
async def update_navigation(session_id: str, payload: NavigationUpdateRequest):
    return NavigationUpdateResponse(
        status="on_track",
        next_instruction="Continue straight for 200m",
        distance_to_destination_m=1800,
        eta=datetime.utcnow(),
        safety_alert=None,
        current_segment_score=88.0,
    )

@app.post("/v1/navigation/{session_id}/end",
          response_model=NavigationEndResponse,
          tags=["Routing"])
async def end_navigation(session_id: str, payload: NavigationEndRequest):
    summary = SessionSummary(
        total_distance_m=2450,
        total_duration_s=1680,
        route_deviations=0,
        avg_safety_score=87.5,
        started_at=datetime.utcnow(),
        ended_at=payload.actual_arrival_time,
    )
    return NavigationEndResponse(
        session_id=session_id,
        status="completed",
        session_summary=summary,
    )

# ========= Safety =========

@app.post("/v1/safety/score-route",
          response_model=ScoreRouteResponse,
          tags=["Safety"])
async def score_route(payload: ScoreRouteRequest):
    now = datetime.utcnow()
    segments = []
    for i, seg in enumerate(payload.segments):
        segments.append(
            SafetySegmentScore(
                segment_id=f"seg_{i+1:03d}",
                start_lat=seg.start_lat,
                start_lon=seg.start_lon,
                end_lat=seg.end_lat,
                end_lon=seg.end_lon,
                score=85 + i,
                risk_factors=[],
            )
        )
    return ScoreRouteResponse(
        overall_score=87.5,
        scoring_breakdown={
            "cctv_coverage": 90,
            "street_lighting": 85,
            "business_activity": 88,
            "crime_rate": 82,
            "pedestrian_traffic": 89,
        },
        segments=segments,
        alerts=[],
        calculated_at=now,
    )

@app.post("/v1/safety/factors",
          response_model=SafetyFactorsResponse,
          tags=["Safety"])
async def get_safety_factors(payload: SafetyFactorsRequest):
    now = datetime.utcnow()
    return SafetyFactorsResponse(
        location=Point(lat=payload.lat, lon=payload.lon),
        radius_m=payload.radius_m,
        factors={
            "cctv_cameras": 3,
            "street_lights": 5,
            "open_businesses": 2,
            "historical_incidents": 0,
            "foot_traffic_level": "medium",
        },
        composite_score=88.0,
        queried_at=now,
    )

@app.put("/v1/safety/weights",
         response_model=SafetyWeightsResponse,
         tags=["Safety"])
async def update_safety_weights(payload: SafetyWeightsRequest):
    total = (
        payload.weights.cctv_coverage
        + payload.weights.street_lighting
        + payload.weights.business_activity
        + payload.weights.crime_rate
        + payload.weights.pedestrian_traffic
    )
    now = datetime.utcnow()
    return SafetyWeightsResponse(
        status="updated",
        user_id=payload.user_id,
        weights=payload.weights,
        weights_sum=total,
        updated_at=now,
    )

# ========= Feedback =========

@app.post("/v1/feedback/submit",
          response_model=FeedbackSubmitResponse,
          tags=["Feedback"])
async def submit_feedback(payload: FeedbackSubmitRequest):
    now = datetime.utcnow()
    ticket = f"TKT-{now.year}-{uuid.uuid4().hex[:6]}"
    feedback_store[payload.feedback_id] = {
        "feedback_id": payload.feedback_id,
        "ticket_number": ticket,
        "status": "under_review",
        "type": payload.type,
        "severity": payload.severity,
        "created_at": now,
        "updated_at": now,
    }
    return FeedbackSubmitResponse(
        feedback_id=payload.feedback_id,
        status="received",
        ticket_number=ticket,
        spam_check="passed",
        estimated_review_time_h=24,
        created_at=now,
    )

@app.post("/v1/feedback/validate",
          response_model=FeedbackValidateResponse,
          tags=["Feedback"])
async def validate_feedback(payload: FeedbackValidateRequest):
    is_spam = payload.recent_submissions_count > 10
    return FeedbackValidateResponse(
        is_spam=is_spam,
        confidence=0.95 if is_spam else 0.9,
        flags=["high_frequency"] if is_spam else [],
        allow_submission=not is_spam,
        reason="Content appears legitimate"
        if not is_spam
        else "Too many submissions in short period",
    )

@app.get("/v1/feedback/{feedback_id}/status",
         response_model=FeedbackStatusResponse,
         tags=["Feedback"])
async def get_feedback_status(
    feedback_id: str,
    user_id: Optional[str] = Query(None, description="User requesting status"),
):
    fb = feedback_store.get(feedback_id)
    now = datetime.utcnow()
    if not fb:
        fb = {
            "feedback_id": feedback_id,
            "ticket_number": f"TKT-{now.year}-{uuid.uuid4().hex[:6]}",
            "status": "under_review",
            "type": "safety_issue",
            "severity": "high",
            "created_at": now,
            "updated_at": now,
            "admin_response": None,
            "resolution": None,
        }
    return FeedbackStatusResponse(**fb)

# ========= Data Management & Audit =========

@app.post("/v1/audit/log",
          response_model=AuditLogResponse,
          tags=["Data Management & Audit"])
async def create_audit_log(payload: AuditLogRequest):
    now = datetime.utcnow()
    log_id = f"log_{uuid.uuid4().hex[:6]}"
    entry = payload.dict()
    entry["log_id"] = log_id
    audit_logs.append(entry)
    return AuditLogResponse(
        log_id=log_id,
        status="recorded",
        retention_until=datetime(
            now.year + 1, now.month, now.day, now.hour, now.minute, now.second
        ),
        created_at=now,
    )

@app.post("/v1/data/collect",
          response_model=DataCollectResponse,
          tags=["Data Management & Audit"])
async def collect_data(payload: DataCollectRequest):
    now = datetime.utcnow()
    data_batches[payload.batch_id] = {
        "source": payload.source,
        "records": payload.records,
    }
    return DataCollectResponse(
        batch_id=payload.batch_id,
        status="processing",
        records_received=len(payload.records),
        estimated_processing_time_s=30,
        queued_at=now,
    )

@app.post("/v1/data/sanitize",
          response_model=DataSanitizeResponse,
          tags=["Data Management & Audit"])
async def sanitize_data(payload: DataSanitizeRequest):
    now = datetime.utcnow()
    total_records = 12500
    processed = 12480
    errors = total_records - processed
    summary = DataSanitizeSummary(
        null_values_removed=320,
        identifiers_masked=4500,
        outliers_dropped=15,
    )
    return DataSanitizeResponse(
        batch_id=payload.batch_id,
        status="completed",
        total_records=total_records,
        processed_records=processed,
        errors=errors,
        start_time=now,
        end_time=now,
        applied_rules=payload.rules,
        summary=summary,
    )

@app.post("/v1/audit/query",
          response_model=AuditQueryResponse,
          tags=["Data Management & Audit"])
async def query_audit_logs(payload: AuditQueryRequest):
    filtered: List[AuditLogEntry] = []
    for e in audit_logs:
        ts = e.get("timestamp", datetime.utcnow())
        if not (payload.date_from <= ts <= payload.date_to):
            continue
        if payload.user_id and e.get("user_id") != payload.user_id:
            continue
        if payload.event_type and e.get("event_type") != payload.event_type:
            continue
        if payload.component and e.get("component") != payload.component:
            continue
        filtered.append(
            AuditLogEntry(
                log_id=e.get("log_id", "unknown"),
                timestamp=ts,
                user_id=e.get("user_id"),
                event_type=e.get("event_type"),
                component=e.get("component"),
                action=e.get("action"),
                details=str(e.get("metadata", "")),
            )
        )
    start = payload.offset
    end = start + payload.limit
    page = filtered[start:end]
    return AuditQueryResponse(
        total_results=len(filtered),
        limit=payload.limit,
        offset=payload.offset,
        logs=page,
    )

# ========= External Emergency (SOS) =========

@app.post("/v1/emergency/call",
          response_model=EmergencyCallResponse,
          tags=["External Emergency (SOS)"])
async def emergency_call(payload: EmergencyCallRequest):
    call_id = f"CALL-{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    emergency_status[payload.sos_id] = {
        "sos_id": payload.sos_id,
        "call_status": "initiated",
        "sms_status": "not_sent",
        "last_update": now,
    }
    return EmergencyCallResponse(
        status="initiated",
        call_id=call_id,
        timestamp=now,
    )

@app.post("/v1/emergency/sms",
          response_model=EmergencySMSResponse,
          tags=["External Emergency (SOS)"])
async def emergency_sms(payload: EmergencySMSRequest):
    sms_id = f"SMS-{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()
    status_entry = emergency_status.setdefault(
        payload.sos_id,
        {
            "sos_id": payload.sos_id,
            "call_status": "not_triggered",
            "sms_status": "not_sent",
            "last_update": now,
        },
    )
    status_entry["sms_status"] = "sent"
    status_entry["last_update"] = now
    return EmergencySMSResponse(
        status="sent",
        sms_id=sms_id,
        timestamp=now,
    )

@app.get("/v1/emergency/{sos_id}/status",
         response_model=EmergencyStatusResponse,
         tags=["External Emergency (SOS)"])
async def get_emergency_status(
    sos_id: str = Path(..., description="SOS event to check")
):
    status = emergency_status.get(sos_id)
    now = datetime.utcnow()
    if not status:
        status = {
            "sos_id": sos_id,
            "call_status": "not_triggered",
            "sms_status": "not_sent",
            "last_update": now,
        }
    return EmergencyStatusResponse(
        sos_id=status["sos_id"],
        call_status=status["call_status"],
        sms_status=status["sms_status"],
        last_update=status["last_update"],
    )
