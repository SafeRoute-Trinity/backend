# Run:
# uvicorn services.data_cleaner.main:app --host 0.0.0.0 --port 20005 --reload
# Docs: http://127.0.0.1:20005/docs

from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="Data Cleaner & Audit Service",
    version="1.0.0",
    description="Data collect/sanitize + audit log/query.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BATCHES = []
AUDIT = []


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
    queued_at: datetime


class DataSanitizeRequest(BaseModel):
    batch_id: str
    rules: List[str]


class DataSanitizeResponse(BaseModel):
    batch_id: str
    status: Literal["completed"]
    total_records: int
    processed_records: int
    errors: int
    start_time: datetime
    end_time: datetime
    applied_rules: List[str]
    summary: Dict[str, int]


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
    created_at: datetime


class AuditQueryRequest(BaseModel):
    user_id: Optional[str] = None
    event_type: Optional[str] = None
    component: Optional[str] = None
    date_from: datetime
    date_to: datetime
    limit: int = 100
    offset: int = 0


class AuditEntry(BaseModel):
    log_id: str
    timestamp: datetime
    user_id: Optional[str]
    event_type: str
    component: str
    action: str
    details: str


class AuditQueryResponse(BaseModel):
    total_results: int
    limit: int
    offset: int
    logs: List[AuditEntry]


@app.get("/")
async def root():
    return {"service": "data_cleaner", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "data_cleaner", "test": "ok", "som": "not"}


@app.post("/v1/data/collect", response_model=DataCollectResponse)
async def collect(body: DataCollectRequest):
    now = datetime.utcnow()
    BATCHES.append(body.dict())
    return DataCollectResponse(
        batch_id=body.batch_id,
        status="processing",
        records_received=len(body.records),
        queued_at=now,
    )


@app.post("/v1/data/sanitize", response_model=DataSanitizeResponse)
async def sanitize(body: DataSanitizeRequest):
    now = datetime.utcnow()
    total = 12500
    processed = 12480
    errors = total - processed
    return DataSanitizeResponse(
        batch_id=body.batch_id,
        status="completed",
        total_records=total,
        processed_records=processed,
        errors=errors,
        start_time=now,
        end_time=now,
        applied_rules=body.rules,
        summary={
            "null_values_removed": 320,
            "identifiers_masked": 4500,
            "outliers_dropped": 15,
        },
    )


@app.post("/v1/audit/log", response_model=AuditLogResponse)
async def audit_log(body: AuditLogRequest):
    log_id = f"log_{len(AUDIT)+1:06d}"
    AUDIT.append({**body.dict(), "log_id": log_id})
    return AuditLogResponse(
        log_id=log_id, status="recorded", created_at=datetime.utcnow()
    )


@app.post("/v1/audit/query", response_model=AuditQueryResponse)
async def audit_query(body: AuditQueryRequest):
    data = []
    for e in AUDIT:
        ts = e["timestamp"]
        if not (body.date_from <= ts <= body.date_to):
            continue
        if body.user_id and e.get("user_id") != body.user_id:
            continue
        if body.event_type and e.get("event_type") != body.event_type:
            continue
        if body.component and e.get("component") != body.component:
            continue
        data.append(
            AuditEntry(
                log_id=e["log_id"],
                timestamp=ts,
                user_id=e.get("user_id"),
                event_type=e["event_type"],
                component=e["component"],
                action=e["action"],
                details=str(e.get("metadata", "")),
            )
        )
    page = data[body.offset : body.offset + body.limit]
    return AuditQueryResponse(
        total_results=len(data), limit=body.limit, offset=body.offset, logs=page
    )
