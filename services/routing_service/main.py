# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import time
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from fastapi import HTTPException, Query, status
from pydantic import BaseModel

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
        print(f"✅ Loaded .env from {env_path}")
    else:
        # Try backend root directory
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            print(f"✅ Loaded .env from {env_path}")
        else:
            load_dotenv()  # Load from default location
            print("⚠️  Attempted to load .env from default location")
except ImportError:
    print("⚠️  python-dotenv not installed, .env file will not be loaded")
except Exception as e:
    print(f"⚠️  Failed to load .env file: {e}")

try:
    # Try relative imports first (when run as module)
    from .mapbox_converter import (
        convert_ors_isochrone_to_mapbox,
        convert_ors_route_to_mapbox,
    )
    from .openrouteservice_client import get_ors_client
except ImportError:
    # Fall back to absolute imports (when run directly)
    from mapbox_converter import (
        convert_ors_isochrone_to_mapbox,
        convert_ors_route_to_mapbox,
    )
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


@app.post("/v1/routes/calculate", response_model=RouteCalculateResponse)
async def calc(body: RouteCalculateRequest):
    # Business metric: initial route calculation
    ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()

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
            Waypoint(lat=body.destination.lat, lon=body.destination.lon, instruction="Arrive"),
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
    # Business metric: route recalculation
    ROUTING_ROUTE_RECALCULATIONS_TOTAL.inc()

    # For mock purposes, reuse calc with current_location as both origin & dest
    return await calc(
        RouteCalculateRequest(
            origin=body.current_location,
            destination=body.current_location,
            user_id="demo",
            preferences=RoutePreferences(optimize_for="balanced", transport_mode="walking"),
        )
    )


@app.post("/v1/navigation/start", response_model=NavigationStartResponse)
async def nav_start(body: NavigationStartRequest):
    # Business metric: navigation session started
    ROUTING_NAVIGATION_STARTS_TOTAL.inc()

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
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
