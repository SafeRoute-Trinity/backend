# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, Query, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

# Prefer the centralized DB factory if available on newer branches; fall back to
# the legacy postgis_db dependency for this branch. This allows the service to
# work both when the global database factory exists (in main) and when it does
# not (in older branches).
# try:
#     # newer main branch may expose a unified postgis/session factory inside libs.db
#     from libs.db import get_postgis_db  # type: ignore
# except Exception:
#     from libs.postgis_db import get_postgis_db
from models.audit import Audit

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)

# Load .env file at startup (before other imports that need env vars)
try:
    from dotenv import load_dotenv

    # Try to find .env file
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded .env from {env_path}")
    else:
        # Try backend root directory
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            print(f"Loaded .env from {env_path}")
        else:
            load_dotenv()  # Load from default location
            print("Attempted to load .env from default location")
except ImportError:
    print("python-dotenv not installed, .env file will not be loaded")
except Exception as e:
    print(f"Failed to load .env file: {e}")

try:
    # Try relative imports first (when run as module)
    from .openrouteservice_client import get_ors_client
except ImportError:
    # Fall back to absolute imports (when run directly)
    from openrouteservice_client import get_ors_client

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create service configuration
service_config = ServiceAppConfig(
    title="Routing Service",
    description="Route calculation & navigation session APIs.",
    service_name="routing_service",
    cors_config=CORSMiddlewareConfig(),
)

from libs.db import DatabaseType, get_database_factory, initialize_databases

# Initialize database connections
initialize_databases([DatabaseType.POSTGRES, DatabaseType.POSTGIS])

# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)
get_postgis_db = db_factory.get_session_dependency(DatabaseType.POSTGIS)

# Create factory and build app
factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

# Add business-specific metrics
ROUTING_ROUTE_CALCULATIONS_TOTAL = factory.add_business_metric(
    "routing_route_calculations_total",
    "Total number of initial route calculation requests",
)

ROUTING_ROUTE_RECALCULATIONS_TOTAL = factory.add_business_metric(
    "routing_route_recalculations_total",
    "Total number of route recalculation requests",
)

ROUTING_NAVIGATION_STARTS_TOTAL = factory.add_business_metric(
    "routing_navigation_starts_total",
    "Total number of navigation sessions started",
)

ROUTES = {}
NAV = {}

# ========= Metrics =========

SERVICE_NAME = "routing_service"
registry = CollectorRegistry()

# Generic per-request counter (shared schema across services)
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

# Business metrics for routing service
ROUTING_ROUTE_CALCULATIONS_TOTAL = Counter(
    "routing_route_calculations_total",
    "Total number of initial route calculation requests",
    registry=registry,
)

ROUTING_ROUTE_RECALCULATIONS_TOTAL = Counter(
    "routing_route_recalculations_total",
    "Total number of route recalculation requests",
    registry=registry,
)


ROUTING_NAVIGATION_STARTS_TOTAL = Counter(
    "routing_navigation_starts_total",
    "Total number of navigation sessions started",
    registry=registry,
)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """
    Track:
    - request count
    - latency per path
    for every HTTP request handled by this service.
    """
    start = time.time()
    response = await call_next(request)

    path = request.url.path

    REQUEST_COUNT.labels(
        service=SERVICE_NAME,
        method=request.method,
        path=path,
        http_status=response.status_code,
    ).inc()

    REQUEST_LATENCY.labels(
        service=SERVICE_NAME,
        path=path,
    ).observe(time.time() - start)

    return response


class Point(BaseModel):
    lat: float
    lon: float


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


class RouteCalculateResponse(BaseModel):
    route_id: uuid.UUID
    routes: List[RouteOption]
    alternatives_count: int
    calculated_at: datetime


class RecalculateRequest(BaseModel):
    route_id: uuid.UUID
    current_location: Point
    reason: Literal["off_track", "road_closure", "user_request", "safety_alert"]


class NavigationStartRequest(BaseModel):
    route_id: uuid.UUID
    user_id: str
    estimated_arrival: datetime


class NavigationStartResponse(BaseModel):
    session_id: uuid.UUID
    status: Literal["active"]
    started_at: datetime


# ---------- Pagination & filters (list response convention) ----------


class PaginationMeta(BaseModel):
    """Metadata for paginated list responses."""

    page: int = Field(..., ge=1, description="Current page (1-based)")
    page_size: int = Field(..., ge=1, le=100, description="Items per page")
    total: int = Field(..., ge=0, description="Total number of items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")


def _total_pages(total: int, page_size: int) -> int:
    return max(0, (total + page_size - 1) // page_size) if page_size > 0 else 0


class RouteListItem(BaseModel):
    """Single route entry for list response."""

    route_id: uuid.UUID
    routes: List[RouteOption]
    alternatives_count: int
    calculated_at: datetime
    user_id: Optional[uuid.UUID] = None


class RouteListResponse(BaseModel):
    """Paginated list of routes with filters."""

    data: List[RouteListItem]
    filters: Dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationMeta


class NavigationSessionItem(BaseModel):
    """Single navigation session for list response."""

    session_id: uuid.UUID
    route_id: uuid.UUID
    user_id: uuid.UUID
    started_at: datetime
    status: Literal["active"] = "active"


class NavigationSessionListResponse(BaseModel):
    """Paginated list of navigation sessions with filters."""

    data: List[NavigationSessionItem]
    filters: Dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationMeta


@app.get("/")
async def root():
    return {"service": "routing_service", "status": "running"}


@app.get("/health")
async def health():
    """Health check endpoint with OpenRouteService status."""
    import os

    ors_client = get_ors_client()
    ors_enabled = ors_client._is_enabled()

    # Check environment variable for debugging
    env_key_exists = os.getenv("ORS_API_KEY") is not None
    env_key_set = bool(os.getenv("ORS_API_KEY"))

    return {
        "status": "ok",
        "service": "routing_service",
        "openrouteservice": "enabled" if ors_enabled else "disabled",
        "openrouteservice_env_check": {
            "ORS_API_KEY_exists": env_key_exists,
            "ORS_API_KEY_set": env_key_set,
        },
    }


@app.get("/v1/routes", response_model=RouteListResponse)
async def list_routes(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    user_id: Optional[uuid.UUID] = Query(None, description="Filter by user ID"),
    calculated_after: Optional[datetime] = Query(
        None, description="Filter: calculated_at >= value"
    ),
    calculated_before: Optional[datetime] = Query(
        None, description="Filter: calculated_at <= value"
    ),
):
    """
    List stored routes with pagination and filters. Response: data, filters, pagination.
    """
    filters_resp: Dict[str, Any] = {
        "user_id": str(user_id) if user_id is not None else "",
        "calculated_after": calculated_after.isoformat() if calculated_after else "",
        "calculated_before": calculated_before.isoformat() if calculated_before else "",
    }
    items = []
    for rid, entry in ROUTES.items():
        if user_id is not None and entry.get("user_id") != user_id:
            continue
        calc_at = entry.get("calculated_at")
        if isinstance(calc_at, datetime):
            if calculated_after is not None and calc_at < calculated_after:
                continue
            if calculated_before is not None and calc_at > calculated_before:
                continue
        items.append(
            {
                "route_id": rid,
                "routes": entry["routes"],
                "alternatives_count": entry["alternatives_count"],
                "calculated_at": entry["calculated_at"],
                "user_id": entry.get("user_id"),
            }
        )
    items.sort(
        key=lambda x: (
            x["calculated_at"] if isinstance(x["calculated_at"], datetime) else datetime.min
        ),
        reverse=True,
    )
    total = len(items)
    offset = (page - 1) * page_size
    page_items = items[offset : offset + page_size]
    data = [RouteListItem(**it) for it in page_items]
    return RouteListResponse(
        data=data,
        filters=filters_resp,
        pagination=PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        ),
    )


@app.post("/v1/routes/calculate", response_model=RouteCalculateResponse)
async def calc(body: RouteCalculateRequest, db=Depends(get_db), postgisDB=Depends(get_postgis_db)):
    # Business metric: initial route calculation
    ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()

    # Business metric: initial route calculation
    ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()

    rid = uuid.uuid4()
    now = datetime.utcnow()
    opt = RouteOption(
        route_index=0,
        is_primary=True,
        geometry="encoded_polyline_demo",
        distance_m=2450,
        duration_s=1800,
        safety_score=87.5,
        waypoints=[
            Waypoint(lat=body.origin.lat, lon=body.origin.lon, instruction="Start"),
            Waypoint(lat=body.destination.lat, lon=body.destination.lon, instruction="Arrive"),
        ],
    )
    ROUTES[rid] = {
        "route_id": rid,
        "routes": [opt],
        "alternatives_count": 1,
        "calculated_at": now,
        "user_id": body.user_id,
    }

    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=body.user_id,
        event_type="routing",
        event_id=rid,
        message="calculate",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    return RouteCalculateResponse(**{k: v for k, v in ROUTES[rid].items() if k != "user_id"})


@app.post("/v1/routes/{route_id}/recalculate", response_model=RouteCalculateResponse)
async def recalc(
    route_id: str, body: RecalculateRequest, db=Depends(get_db), postgisDB=Depends(get_postgis_db)
):
    # TODO: should use actual route_id (uuid) and test AUDIT again

    # Business metric: route recalculation
    ROUTING_ROUTE_RECALCULATIONS_TOTAL.inc()

    # For mock purposes, reuse calc with current_location as both origin & dest
    # Business metric: route recalculation
    ROUTING_ROUTE_RECALCULATIONS_TOTAL.inc()

    # For mock purposes, reuse calc with current_location as both origin & dest
    return await calc(
        RouteCalculateRequest(
            origin=body.current_location,
            destination=body.current_location,
            user_id="recalc-placeholder",
            preferences=RoutePreferences(optimize_for="balanced", transport_mode="walking"),
        )
    )


@app.post("/v1/navigation/start", response_model=NavigationStartResponse)
async def nav_start(
    body: NavigationStartRequest, db=Depends(get_db), postgisDB=Depends(get_postgis_db)
):
    # TODO: should use actual route_id and user_id (uuid) and test AUDIT again

    # Business metric: navigation session started
    ROUTING_NAVIGATION_STARTS_TOTAL.inc()

    # Business metric: navigation session started
    ROUTING_NAVIGATION_STARTS_TOTAL.inc()

    sid = uuid.uuid4()
    now = datetime.utcnow()
    NAV[sid] = {"route_id": body.route_id, "user_id": body.user_id, "started_at": now}

    audit = Audit(
        log_id=uuid.uuid4(),
        user_id=body.user_id,
        event_type="routing",
        event_id=body.route_id,
        message="calculate",
        created_at=now,
        updated_at=now,
    )
    db.add(audit)

    return NavigationStartResponse(session_id=sid, status="active", started_at=now)


@app.get("/v1/navigation/sessions", response_model=NavigationSessionListResponse)
async def list_navigation_sessions(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    user_id: Optional[uuid.UUID] = Query(None, description="Filter by user ID"),
    route_id: Optional[uuid.UUID] = Query(None, description="Filter by route ID"),
    started_after: Optional[datetime] = Query(None, description="Filter: started_at >= value"),
    started_before: Optional[datetime] = Query(None, description="Filter: started_at <= value"),
):
    """
    List navigation sessions with pagination and filters. Response: data, filters, pagination.
    """
    filters_resp: Dict[str, Any] = {
        "user_id": str(user_id) if user_id is not None else "",
        "route_id": str(route_id) if route_id is not None else "",
        "started_after": started_after.isoformat() if started_after else "",
        "started_before": started_before.isoformat() if started_before else "",
    }
    items = []
    for sid, entry in NAV.items():
        if user_id is not None and entry.get("user_id") != user_id:
            continue
        if route_id is not None and entry.get("route_id") != route_id:
            continue
        started_at = entry.get("started_at")
        if isinstance(started_at, datetime):
            if started_after is not None and started_at < started_after:
                continue
            if started_before is not None and started_at > started_before:
                continue
        items.append(
            {
                "session_id": sid,
                "route_id": entry["route_id"],
                "user_id": entry["user_id"],
                "started_at": entry["started_at"],
                "status": "active",
            }
        )
    items.sort(
        key=lambda x: x["started_at"] if isinstance(x["started_at"], datetime) else datetime.min,
        reverse=True,
    )
    total = len(items)
    offset = (page - 1) * page_size
    page_items = items[offset : offset + page_size]
    data = [NavigationSessionItem(**it) for it in page_items]
    return NavigationSessionListResponse(
        data=data,
        filters=filters_resp,
        pagination=PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        ),
    )


@app.get("/metrics")
async def metrics():
    """
    Expose Prometheus metrics for this Routing service.
    """
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
