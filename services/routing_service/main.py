# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import time
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Histogram,
                               generate_latest)
from pydantic import BaseModel

app = FastAPI(
    title="Routing Service",
    version="1.0.0",
    description="Route calculation & navigation session APIs.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ROUTES = {}
NAV = {}

# ========= Metrics =========

SERVICE_NAME = "routing_service"

# Generic per-request counter (shared schema across services)
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

# Business metrics for routing service
ROUTING_ROUTE_CALCULATIONS_TOTAL = Counter(
    "routing_route_calculations_total",
    "Total number of initial route calculation requests",
)

ROUTING_ROUTE_RECALCULATIONS_TOTAL = Counter(
    "routing_route_recalculations_total",
    "Total number of route recalculation requests",
)

ROUTING_NAVIGATION_STARTS_TOTAL = Counter(
    "routing_navigation_starts_total",
    "Total number of navigation sessions started",
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
    route_id: str
    routes: List[RouteOption]
    alternatives_count: int
    calculated_at: datetime


class RecalculateRequest(BaseModel):
    route_id: str
    current_location: Point
    reason: Literal["off_track", "road_closure", "user_request", "safety_alert"]


class NavigationStartRequest(BaseModel):
    route_id: str
    user_id: str
    estimated_arrival: datetime


class NavigationStartResponse(BaseModel):
    session_id: str
    status: Literal["active"]
    started_at: datetime


@app.get("/")
async def root():
    return {"service": "routing_service", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "routing_service"}


@app.post("/v1/routes/calculate", response_model=RouteCalculateResponse)
async def calc(body: RouteCalculateRequest):
    # Business metric: initial route calculation
    ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()

    rid = f"rt_{uuid.uuid4().hex[:6]}"
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
            Waypoint(
                lat=body.destination.lat, lon=body.destination.lon, instruction="Arrive"
            ),
        ],
    )
    ROUTES[rid] = {
        "route_id": rid,
        "routes": [opt],
        "alternatives_count": 1,
        "calculated_at": now,
    }
    return RouteCalculateResponse(**ROUTES[rid])


@app.post("/v1/routes/{route_id}/recalculate", response_model=RouteCalculateResponse)
async def recalc(route_id: str, body: RecalculateRequest):
    # Business metric: route recalculation
    ROUTING_ROUTE_RECALCULATIONS_TOTAL.inc()

    # For mock purposes, reuse calc with current_location as both origin & dest
    return await calc(
        RouteCalculateRequest(
            origin=body.current_location,
            destination=body.current_location,
            user_id="demo",
            preferences=RoutePreferences(
                optimize_for="balanced", transport_mode="walking"
            ),
        )
    )


@app.post("/v1/navigation/start", response_model=NavigationStartResponse)
async def nav_start(body: NavigationStartRequest):
    # Business metric: navigation session started
    ROUTING_NAVIGATION_STARTS_TOTAL.inc()

    sid = f"nav_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    NAV[sid] = {"route_id": body.route_id, "user_id": body.user_id, "started_at": now}
    return NavigationStartResponse(session_id=sid, status="active", started_at=now)


@app.get("/metrics")
async def metrics():
    """
    Expose Prometheus metrics for this Routing service.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
