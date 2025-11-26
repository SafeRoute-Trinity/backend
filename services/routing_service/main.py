# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import logging
import time
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel

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
    ors_client = get_ors_client()
    ors_enabled = ors_client._is_enabled()
    return {
        "status": "ok",
        "service": "routing_service",
        "openrouteservice": "enabled" if ors_enabled else "disabled",
    }


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
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


# ========= OpenRouteService Integration =========


@app.get("/route")
async def get_route(
    start: str = Query(..., description="Start coordinates as 'lat,lon'"),
    end: str = Query(..., description="End coordinates as 'lat,lon'"),
    profile: str = Query(
        "driving-car",
        description="Routing profile: driving-car, foot-walking, cycling-regular, etc.",
    ),
):
    """
    Get route from OpenRouteService and convert to Mapbox-compatible format.

    Args:
        start: Start coordinates as "lat,lon"
        end: End coordinates as "lat,lon"
        profile: Routing profile (default: driving-car)

    Returns:
        Mapbox-compatible GeoJSON FeatureCollection with route LineString
    """
    try:
        # Parse coordinates
        try:
            start_coords = tuple(map(float, start.split(",")))
            end_coords = tuple(map(float, end.split(",")))
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid coordinate format. Expected 'lat,lon'. Error: {e}",
            )

        # Validate coordinates
        if not (-90 <= start_coords[0] <= 90 and -180 <= start_coords[1] <= 180):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid start coordinates. Latitude must be -90 to 90, longitude -180 to 180.",
            )
        if not (-90 <= end_coords[0] <= 90 and -180 <= end_coords[1] <= 180):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid end coordinates. Latitude must be -90 to 90, longitude -180 to 180.",
            )

        # Get OpenRouteService client
        ors_client = get_ors_client()
        if not ors_client._is_enabled():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OpenRouteService is not configured. Please set ORS_API_KEY environment variable.",
            )

        # Call OpenRouteService
        logger.info(
            f"Requesting route: start=({start_coords[0]}, {start_coords[1]}), "
            f"end=({end_coords[0]}, {end_coords[1]}), profile={profile}"
        )

        ors_response = await ors_client.get_directions(
            start=start_coords, end=end_coords, profile=profile
        )

        if not ors_response:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to get route from OpenRouteService. Please check logs for details.",
            )

        # Convert to Mapbox format
        mapbox_response = convert_ors_route_to_mapbox(ors_response)

        if not mapbox_response:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to convert route to Mapbox format.",
            )

        logger.info(
            f"Successfully returned route with {len(mapbox_response.get('features', []))} features"
        )
        return mapbox_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in /route endpoint: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )


@app.get("/isochrone")
async def get_isochrone(
    location: str = Query(..., description="Location coordinates as 'lat,lon'"),
    profile: str = Query(
        "driving-car",
        description="Routing profile: driving-car, foot-walking, cycling-regular, etc.",
    ),
    range: str = Query(
        "600,1200,1800",
        description="Comma-separated list of ranges in seconds (for time) or meters (for distance)",
    ),
    range_type: str = Query("time", description="Range type: 'time' or 'distance'"),
):
    """
    Get isochrones from OpenRouteService and convert to Mapbox-compatible format.

    Args:
        location: Location coordinates as "lat,lon"
        profile: Routing profile (default: driving-car)
        range: Comma-separated list of ranges (default: "600,1200,1800" seconds)
        range_type: "time" or "distance" (default: "time")

    Returns:
        Mapbox-compatible GeoJSON FeatureCollection with isochrone Polygons
    """
    try:
        # Parse coordinates
        try:
            location_coords = tuple(map(float, location.split(",")))
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid coordinate format. Expected 'lat,lon'. Error: {e}",
            )

        # Validate coordinates
        if not (-90 <= location_coords[0] <= 90 and -180 <= location_coords[1] <= 180):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid location coordinates. Latitude must be -90 to 90, longitude -180 to 180.",
            )

        # Parse ranges
        try:
            range_list = [int(r.strip()) for r in range.split(",")]
            if not range_list:
                raise ValueError("Range list cannot be empty")
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid range format. Expected comma-separated integers. Error: {e}",
            )

        # Validate range_type
        if range_type not in ["time", "distance"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid range_type. Must be 'time' or 'distance'.",
            )

        # Get OpenRouteService client
        ors_client = get_ors_client()
        if not ors_client._is_enabled():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OpenRouteService is not configured. Please set ORS_API_KEY environment variable.",
            )

        # Call OpenRouteService
        logger.info(
            f"Requesting isochrone: location=({location_coords[0]}, {location_coords[1]}), "
            f"profile={profile}, range={range_list}, range_type={range_type}"
        )

        ors_response = await ors_client.get_isochrones(
            location=location_coords,
            profile=profile,
            range=range_list,
            range_type=range_type,
        )

        if not ors_response:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to get isochrone from OpenRouteService. Please check logs for details.",
            )

        # Convert to Mapbox format
        mapbox_response = convert_ors_isochrone_to_mapbox(ors_response)

        if not mapbox_response:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to convert isochrone to Mapbox format.",
            )

        logger.info(
            f"Successfully returned isochrone with {len(mapbox_response.get('features', []))} features"
        )
        return mapbox_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in /isochrone endpoint: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )
