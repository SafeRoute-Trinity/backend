# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, HTTPException, Query, Request, Response
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

ROUTE_SUBGRAPH_EXPAND_DEGREES = float(os.getenv("ROUTE_SUBGRAPH_EXPAND_DEGREES", "0.01"))
ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES = float(os.getenv("ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES", "0.08"))
ROUTE_DEFAULT_ALGORITHM: Literal["astar", "dijkstra", "bd_dijkstra"] = "dijkstra"
WALKING_SPEED_MPS = 1.39

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


class Coordinate(BaseModel):
    lat: float
    lng: float


class RouteRequest(BaseModel):
    start: Coordinate
    end: Coordinate


class WeightUpdateRequest(BaseModel):
    edge_id: int
    safety_factor: float


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


def _routing_algorithm_from_preferences(
    optimize_for: Literal["safety", "time", "distance", "balanced"],
) -> Literal["astar", "dijkstra", "bd_dijkstra"]:
    if optimize_for == "time":
        return "astar"
    if optimize_for == "distance":
        return "bd_dijkstra"
    return ROUTE_DEFAULT_ALGORITHM


def _extract_waypoints_from_geojson(
    route_geojson: Dict[str, Any], origin: Point, destination: Point
) -> List[Waypoint]:
    features = route_geojson.get("features", [])
    road_coords: List[List[float]] = []

    for feature in features:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        if geometry.get("type") != "LineString":
            continue
        if properties.get("type") in (None, "road", "connector"):
            road_coords.extend(geometry.get("coordinates") or [])

    if len(road_coords) < 2:
        return [
            Waypoint(lat=origin.lat, lon=origin.lon, instruction="Start"),
            Waypoint(lat=destination.lat, lon=destination.lon, instruction="Arrive"),
        ]

    max_points = 12
    step = max(1, len(road_coords) // max_points)
    sampled = [road_coords[i] for i in range(0, len(road_coords), step)]
    if sampled[-1] != road_coords[-1]:
        sampled.append(road_coords[-1])

    waypoints: List[Waypoint] = []
    for idx, coord in enumerate(sampled):
        if not isinstance(coord, list) or len(coord) < 2:
            continue
        lon, lat = coord[0], coord[1]
        instruction = None
        if idx == 0:
            instruction = "Start"
        elif idx == len(sampled) - 1:
            instruction = "Arrive"
        waypoints.append(Waypoint(lat=lat, lon=lon, instruction=instruction))

    if len(waypoints) < 2:
        return [
            Waypoint(lat=origin.lat, lon=origin.lon, instruction="Start"),
            Waypoint(lat=destination.lat, lon=destination.lon, instruction="Arrive"),
        ]
    return waypoints


async def _compute_weighted_route_geojson(
    request: RouteRequest,
    db: AsyncSession,
    algorithm: Literal["astar", "dijkstra", "bd_dijkstra"] = "dijkstra",
) -> Dict[str, Any]:
    node_query = text("""
        WITH p AS (
            SELECT ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS pt
        ),
        nearest_edge AS (
            SELECT w.source, w.target, w.geometry, p.pt
            FROM ways w
            CROSS JOIN p
            ORDER BY w.geometry <-> p.pt
            LIMIT 1
        )
        SELECT
            CASE
                WHEN ST_Distance(ST_StartPoint(geometry)::geography, pt::geography)
                   <= ST_Distance(ST_EndPoint(geometry)::geography, pt::geography)
                THEN source
                ELSE target
            END AS id
        FROM nearest_edge;
        """)

    start_node_res = (
        await db.execute(node_query, {"lng": request.start.lng, "lat": request.start.lat})
    ).fetchone()
    end_node_res = (
        await db.execute(node_query, {"lng": request.end.lng, "lat": request.end.lat})
    ).fetchone()

    if not start_node_res or not end_node_res:
        raise HTTPException(status_code=404, detail="Could not find nearest road nodes.")

    start_node = start_node_res[0]
    end_node = end_node_res[0]

    routing_fn = (
        "pgr_aStar"
        if algorithm == "astar"
        else "pgr_bdDijkstra" if algorithm == "bd_dijkstra" else "pgr_dijkstra"
    )
    routing_query = text(
        """
        SELECT
            d.seq,
            d.path_seq,
            d.node,
            d.edge,
            d.cost,
            d.agg_cost,
            ST_AsGeoJSON(w.geometry) as geojson,
            w.length,
            w.source,
            w.target
        FROM """
        + routing_fn
        + """(
            CAST(:sql AS TEXT),
            CAST(:start_node AS BIGINT),
            CAST(:end_node AS BIGINT),
            false
        ) as d
        LEFT JOIN ways w ON d.edge = w.gid
        ORDER BY d.seq;
        """
    )

    expanded = max(0.001, ROUTE_SUBGRAPH_EXPAND_DEGREES)
    max_expand = max(expanded, ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES)
    expansions: List[float] = []
    while expanded <= max_expand + 1e-9:
        expansions.append(round(expanded, 6))
        expanded *= 2

    routes = []
    for expand in expansions:
        cost_sql = f"""
            WITH route_window AS (
                SELECT ST_Expand(
                    ST_Envelope(
                        ST_Collect(
                            ST_SetSRID(ST_MakePoint({request.start.lng}, {request.start.lat}), 4326),
                            ST_SetSRID(ST_MakePoint({request.end.lng}, {request.end.lat}), 4326)
                        )
                    ),
                    {expand}
                ) AS bbox
            )
            SELECT
                w.gid AS id,
                w.source,
                w.target,
                w.length * w.safety_factor AS cost,
                w.length * w.safety_factor AS reverse_cost,
                ST_X(ST_StartPoint(w.geometry)) AS x1,
                ST_Y(ST_StartPoint(w.geometry)) AS y1,
                ST_X(ST_EndPoint(w.geometry)) AS x2,
                ST_Y(ST_EndPoint(w.geometry)) AS y2
            FROM ways w
            CROSS JOIN route_window rw
            WHERE w.geometry && rw.bbox
        """
        try:
            route_res = await db.execute(
                routing_query, {"sql": cost_sql, "start_node": start_node, "end_node": end_node}
            )
            routes = route_res.fetchall()
            if routes:
                break
        except Exception:
            continue

    if not routes:
        full_cost_sql = """
            SELECT
                gid AS id,
                source,
                target,
                length * safety_factor AS cost,
                length * safety_factor AS reverse_cost,
                ST_X(ST_StartPoint(geometry)) AS x1,
                ST_Y(ST_StartPoint(geometry)) AS y1,
                ST_X(ST_EndPoint(geometry)) AS x2,
                ST_Y(ST_EndPoint(geometry)) AS y2
            FROM ways
        """
        route_res = await db.execute(
            routing_query,
            {"sql": full_cost_sql, "start_node": start_node, "end_node": end_node},
        )
        routes = route_res.fetchall()

    if not routes:
        raise HTTPException(status_code=404, detail="No path found.")

    features: List[Dict[str, Any]] = []
    total_distance = 0.0
    road_coords: List[List[float]] = []

    for route_row in routes:
        if not route_row.geojson:
            continue
        geom = json.loads(route_row.geojson)
        coords = geom.get("coordinates") or []
        if route_row.node == route_row.target:
            coords = list(reversed(coords))

        if not road_coords:
            road_coords.extend(coords)
        else:
            if road_coords[-1] == coords[0]:
                road_coords.extend(coords[1:])
            else:
                road_coords.extend(coords)

        if route_row.length:
            total_distance += float(route_row.length)

    if not road_coords:
        raise HTTPException(status_code=404, detail="No route geometry found.")

    features.append(
        {
            "type": "Feature",
            "properties": {"type": "connector"},
            "geometry": {
                "type": "LineString",
                "coordinates": [[request.start.lng, request.start.lat], road_coords[0]],
            },
        }
    )
    features.append(
        {
            "type": "Feature",
            "properties": {"type": "road"},
            "geometry": {"type": "LineString", "coordinates": road_coords},
        }
    )
    features.append(
        {
            "type": "Feature",
            "properties": {"type": "connector"},
            "geometry": {
                "type": "LineString",
                "coordinates": [road_coords[-1], [request.end.lng, request.end.lat]],
            },
        }
    )

    duration_seconds = total_distance / WALKING_SPEED_MPS
    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "summary": {
                "distance_meters": total_distance,
                "distance_km": round(total_distance / 1000, 2),
                "duration": duration_seconds,
            }
        },
    }


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


@app.get("/api/danger_zones")
async def get_danger_zones(db: AsyncSession = Depends(get_postgis_db)):
    try:
        query = text("""
            SELECT gid, safety_factor, ST_AsGeoJSON(geometry) AS geojson
            FROM ways
            WHERE safety_factor != 1.0
            """)
        rows = (await db.execute(query)).fetchall()
        features = []
        for row in rows:
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "id": row.gid,
                        "weight": row.safety_factor,
                        "type": "edge",
                    },
                    "geometry": json.loads(row.geojson),
                }
            )
        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        logger.exception("Failed to load danger zones")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/v1/routing-debug/danger-zones")
async def get_danger_zones_v1(db: AsyncSession = Depends(get_postgis_db)):
    return await get_danger_zones(db=db)


@app.post("/api/danger_zones")
async def update_danger_zone(
    update: WeightUpdateRequest, db: AsyncSession = Depends(get_postgis_db)
):
    try:
        geom_query = text("SELECT geometry FROM ways WHERE gid = :id")
        geom_res = (await db.execute(geom_query, {"id": update.edge_id})).fetchone()
        if not geom_res:
            raise HTTPException(status_code=404, detail="Edge not found")

        update_query = text("""
            UPDATE ways
            SET safety_factor = :w
            WHERE ST_Equals(geometry, :geom)
            """)
        await db.execute(update_query, {"w": update.safety_factor, "geom": geom_res[0]})
        await db.commit()
        return {
            "status": "updated",
            "id": update.edge_id,
            "weight": update.safety_factor,
            "note": "Updated bidirectional edges",
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception("Failed to update danger zone")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/v1/routing-debug/danger-zones")
async def update_danger_zone_v1(
    update: WeightUpdateRequest, db: AsyncSession = Depends(get_postgis_db)
):
    return await update_danger_zone(update=update, db=db)


@app.delete("/api/danger_zones/{zone_id}")
async def reset_danger_zone(zone_id: int, db: AsyncSession = Depends(get_postgis_db)):
    try:
        geom_query = text("SELECT geometry FROM ways WHERE gid = :id")
        geom_res = (await db.execute(geom_query, {"id": zone_id})).fetchone()
        if not geom_res:
            raise HTTPException(status_code=404, detail="Edge not found")

        reset_query = text("""
            UPDATE ways
            SET safety_factor = 1.0
            WHERE ST_Equals(geometry, :geom)
            """)
        await db.execute(reset_query, {"geom": geom_res[0]})
        await db.commit()
        return {"status": "reset", "id": zone_id}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception("Failed to reset danger zone")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/v1/routing-debug/danger-zones/{zone_id}")
async def reset_danger_zone_v1(zone_id: int, db: AsyncSession = Depends(get_postgis_db)):
    return await reset_danger_zone(zone_id=zone_id, db=db)


@app.post("/api/route")
async def get_route(
    request: RouteRequest,
    algorithm: Literal["astar", "dijkstra", "bd_dijkstra"] = Query("dijkstra"),
    db: AsyncSession = Depends(get_postgis_db),
):
    return await _compute_weighted_route_geojson(request=request, db=db, algorithm=algorithm)


@app.post("/v1/routing-debug/route")
async def get_route_v1_debug(
    request: RouteRequest,
    algorithm: Literal["astar", "dijkstra", "bd_dijkstra"] = Query("dijkstra"),
    db: AsyncSession = Depends(get_postgis_db),
):
    return await get_route(request=request, algorithm=algorithm, db=db)


@app.get("/api/graph")
async def get_graph_geojson(
    min_lng: float = Query(..., description="Minimum Longitude"),
    min_lat: float = Query(..., description="Minimum Latitude"),
    max_lng: float = Query(..., description="Maximum Longitude"),
    max_lat: float = Query(..., description="Maximum Latitude"),
    db: AsyncSession = Depends(get_postgis_db),
):
    try:
        query = text("""
            SELECT gid, source, target, ST_AsGeoJSON(geometry) AS geojson, safety_factor
            FROM ways
            WHERE geometry && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
            LIMIT 2000
            """)
        rows = (
            await db.execute(
                query,
                {
                    "min_lng": min_lng,
                    "min_lat": min_lat,
                    "max_lng": max_lng,
                    "max_lat": max_lat,
                },
            )
        ).fetchall()

        features = []
        for row in rows:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "edge", "id": row.gid, "weight": row.safety_factor},
                    "geometry": json.loads(row.geojson),
                }
            )
        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        logger.exception("Failed to load graph edges")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/v1/routing-debug/graph")
async def get_graph_geojson_v1(
    min_lng: float = Query(..., description="Minimum Longitude"),
    min_lat: float = Query(..., description="Minimum Latitude"),
    max_lng: float = Query(..., description="Maximum Longitude"),
    max_lat: float = Query(..., description="Maximum Latitude"),
    db: AsyncSession = Depends(get_postgis_db),
):
    return await get_graph_geojson(
        min_lng=min_lng,
        min_lat=min_lat,
        max_lng=max_lng,
        max_lat=max_lat,
        db=db,
    )


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
async def calc(
    body: RouteCalculateRequest,
    db: AsyncSession = Depends(get_db),
    postgisDB: AsyncSession = Depends(get_postgis_db),
):
    ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()

    rid = uuid.uuid4()
    now = datetime.utcnow()
    route_geojson: Optional[Dict[str, Any]] = None

    route_request = RouteRequest(
        start=Coordinate(lat=body.origin.lat, lng=body.origin.lon),
        end=Coordinate(lat=body.destination.lat, lng=body.destination.lon),
    )

    algorithm = _routing_algorithm_from_preferences(body.preferences.optimize_for)

    try:
        route_geojson = await _compute_weighted_route_geojson(
            request=route_request,
            db=postgisDB,
            algorithm=algorithm,
        )
    except Exception as e:
        logger.warning("Falling back to default route in /v1/routes/calculate: %s", e)

    if route_geojson:
        summary = (route_geojson.get("properties") or {}).get("summary") or {}
        distance_meters = int(round(float(summary.get("distance_meters", 0))))
        duration_seconds = int(round(float(summary.get("duration", 0))))
        safety_score = 90.0 if body.preferences.optimize_for == "safety" else 82.0
        opt = RouteOption(
            route_index=0,
            is_primary=True,
            geometry=json.dumps(route_geojson),
            distance_m=max(distance_meters, 0),
            duration_s=max(duration_seconds, 0),
            safety_score=safety_score,
            waypoints=_extract_waypoints_from_geojson(route_geojson, body.origin, body.destination),
        )
    else:
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
    route_id: uuid.UUID,
    body: RecalculateRequest,
    db=Depends(get_db),
    postgisDB=Depends(get_postgis_db),
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
