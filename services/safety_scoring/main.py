# Run:
# uvicorn services.safety_scoring.main:app --host 0.0.0.0 --port 20003 --reload
# Docs: http://127.0.0.1:20003/docs

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Load backend .env for local development convenience.
backend_env_path = Path(__file__).resolve().parents[2] / ".env"
if backend_env_path.exists():
    load_dotenv(backend_env_path)

from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import ServiceAppConfig

# Initialize database factory
initialize_databases([DatabaseType.POSTGIS])

app = FastAPI()

# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGIS)
get_postgis_db = db_factory.get_session_dependency(DatabaseType.POSTGIS)
# Routing tuning knobs (can be overridden by env vars)
ROUTE_SUBGRAPH_EXPAND_DEGREES = float(os.getenv("ROUTE_SUBGRAPH_EXPAND_DEGREES", "0.01"))
ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES = float(os.getenv("ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES", "0.08"))
ROUTE_DEBUG_LOG = os.getenv("ROUTE_DEBUG_LOG", "false").lower() == "true"
GRAPHHOPPER_PROXY_SERVICE_URL = os.getenv(
    "GRAPHHOPPER_PROXY_SERVICE_URL",
    os.getenv("CH_ROUTING_SERVICE_URL", "http://127.0.0.1:20007"),
)
GRAPHHOPPER_PROXY_TIMEOUT_SECONDS = float(
    os.getenv("GRAPHHOPPER_PROXY_TIMEOUT_SECONDS", os.getenv("CH_ROUTING_TIMEOUT_SECONDS", "8"))
)
CH_FALLBACK_TO_DIJKSTRA = os.getenv("CH_FALLBACK_TO_DIJKSTRA", "true").lower() == "true"

# Safety scoring dedicated database URL
# Priority: SAFETY_SCORING_DATABASE_URL > POSTGIS_DATABASE_URL > shared get_db fallback
SAFETY_SCORING_DATABASE_URL = os.getenv("SAFETY_SCORING_DATABASE_URL") or os.getenv(
    "POSTGIS_DATABASE_URL"
)

_SafetyScoringSessionLocal = None
if SAFETY_SCORING_DATABASE_URL:
    _safety_scoring_engine = create_async_engine(
        SAFETY_SCORING_DATABASE_URL,
        echo=False,
        future=True,
    )
    _SafetyScoringSessionLocal = sessionmaker(
        bind=_safety_scoring_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_safety_scoring_db():
    if _SafetyScoringSessionLocal is not None:
        async with _SafetyScoringSessionLocal() as session:
            yield session
        return

    async for shared_session in get_db():
        yield shared_session


async def get_ch_route_geojson(route_request: "RouteRequest") -> dict:
    """
    Fetch route from GraphHopper proxy and return GeoJSON-compatible payload.
    """
    try:
        async with httpx.AsyncClient(timeout=GRAPHHOPPER_PROXY_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{GRAPHHOPPER_PROXY_SERVICE_URL}/api/route",
                params={"algorithm": "ch"},
                json={
                    "start": {"lat": route_request.start.lat, "lng": route_request.start.lng},
                    "end": {"lat": route_request.end.lat, "lng": route_request.end.lng},
                },
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GraphHopper proxy request failed: {e}") from e

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"GraphHopper proxy error: status={response.status_code}, body={response.text[:200]}",
        )

    body = response.json()
    if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
        raise HTTPException(status_code=502, detail="Invalid CH response format.")
    return body


# Create service configuration
service_config = ServiceAppConfig(
    service_name="safety_scoring",
    title="Safety Scoring Service",
    description="Safety scoring, factors, and weights APIs.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files (if needed, though usually served by frontend or nginx)
# app.mount("/static", StaticFiles(directory="../frontend/static"), name="static")

# ========= Metrics =========

SERVICE_NAME = "safety_scoring"
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

# Business metrics for this service
SAFETY_SCORE_ROUTE_REQUESTS_TOTAL = Counter(
    "safety_score_route_requests_total",
    "Total number of safety route scoring requests",
    registry=registry,
)

SAFETY_FACTORS_QUERIES_TOTAL = Counter(
    "safety_factors_queries_total",
    "Total number of safety factors queries",
    registry=registry,
)

SAFETY_WEIGHTS_UPDATES_TOTAL = Counter(
    "safety_weights_updates_total",
    "Total number of safety weights update requests",
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


# ========= Models =========


class PointModel(BaseModel):
    lat: float
    lon: float


class Coordinate(BaseModel):
    lat: float
    lng: float


class WeightUpdateRequest(BaseModel):
    edge_id: int  # gid in ways table
    safety_factor: float


class RouteRequest(BaseModel):
    start: Coordinate
    end: Coordinate


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
    location: PointModel
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
    location: PointModel
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


# ---------- Pagination & filters (list response convention) ----------


class PaginationMeta(BaseModel):
    """Metadata for paginated list responses."""

    page: int = Field(..., ge=1, description="Current page (1-based)")
    page_size: int = Field(..., ge=1, le=500, description="Items per page")
    total: int = Field(..., ge=0, description="Total number of items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")


def _total_pages(total: int, page_size: int) -> int:
    return max(0, (total + page_size - 1) // page_size) if page_size > 0 else 0


# ========= Endpoints =========


@app.get("/")
async def root():
    return {"service": "safety_scoring", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "safety_scoring"}


@app.get("/metrics")
async def metrics():
    """
    Expose Prometheus metrics for this Safety Scoring service.
    """
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


# --- Merged from main copy.py (Converted to Async) ---


@app.get("/api/danger_zones")
async def get_danger_zones(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    min_safety_factor: Optional[float] = Query(
        None, description="Filter: safety_factor >= value (e.g. 0.5)"
    ),
    max_safety_factor: Optional[float] = Query(
        None, description="Filter: safety_factor <= value (e.g. 2.0)"
    ),
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    """
    Return edges that have custom weights. Paginated with optional filters.
    Response follows convention: data, filters, pagination.
    """
    try:
        # Filters for response (empty string when not set)
        filters_resp: Dict[str, Any] = {
            "min_safety_factor": min_safety_factor if min_safety_factor is not None else "",
            "max_safety_factor": max_safety_factor if max_safety_factor is not None else "",
        }
        # SQL: always exclude default weight 1.0; optional range
        where_clause = "safety_factor != 1.0"
        params: Dict[str, Any] = {}
        if min_safety_factor is not None:
            where_clause += " AND safety_factor >= :min_sf"
            params["min_sf"] = min_safety_factor
        if max_safety_factor is not None:
            where_clause += " AND safety_factor <= :max_sf"
            params["max_sf"] = max_safety_factor

        count_query = text(f"SELECT COUNT(*) FROM ways WHERE {where_clause}")
        count_result = await db.execute(count_query, params)
        total = count_result.scalar() or 0

        offset = (page - 1) * page_size
        params["limit"] = page_size
        params["offset"] = offset
        query = text(f"""
            SELECT gid, safety_factor, ST_AsGeoJSON(geometry) as geojson
            FROM ways
            WHERE {where_clause}
            ORDER BY gid
            LIMIT :limit OFFSET :offset
            """)
        result = await db.execute(query, params)
        rows = result.fetchall()

        features = []
        for r in rows:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"id": r.gid, "weight": r.safety_factor, "type": "edge"},
                    "geometry": json.loads(r.geojson),
                }
            )

        pagination = PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        )
        return {
            "type": "FeatureCollection",
            "data": features,
            "features": features,  # backward compat
            "filters": filters_resp,
            "pagination": pagination.model_dump(),
        }
    except Exception as e:
        print(f"Error fetching zones: {e}")
        return {
            "type": "FeatureCollection",
            "data": [],
            "features": [],
            "filters": {"min_safety_factor": "", "max_safety_factor": ""},
            "pagination": PaginationMeta(page=1, page_size=50, total=0, total_pages=0).model_dump(),
        }


@app.post("/api/danger_zones")
async def update_danger_zone(
    update: WeightUpdateRequest,
    db=Depends(get_db),
    postgisDb: AsyncSession = Depends(get_postgis_db),
):
    """
    Update safety weight for a specific edge and its bidirectional counterpart.
    """
    try:
        # Find the geometry of the selected edge
        geom_query = text("SELECT geometry FROM ways WHERE gid = :id")
        result = await postgisDb.execute(geom_query, {"id": update.edge_id})
        geom_res = result.fetchone()

        if not geom_res:
            raise HTTPException(status_code=404, detail="Edge not found")

        # Update ALL edges that share exactly the same geometry (spatial equality)
        update_query = text("""
            UPDATE ways 
            SET safety_factor = :w 
            WHERE ST_Equals(geometry, :geom)
        """)

        await postgisDb.execute(update_query, {"w": update.safety_factor, "geom": geom_res[0]})
        await postgisDb.commit()

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

        # Metric
        SAFETY_WEIGHTS_UPDATES_TOTAL.inc()

        return {
            "status": "updated",
            "id": update.edge_id,
            "safety_factor": update.safety_factor,
            "note": "Updated bidirectional edges",
        }
    except HTTPException:
        raise
    except Exception as e:
        await postgisDb.rollback()
        print(f"Error updating safety_factor: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/danger_zones/{zone_id}")
async def reset_danger_zone(
    zone_id: int,
    db: AsyncSession = Depends(get_db),
    postgisDb: AsyncSession = Depends(get_postgis_db),
):
    """
    Reset safety_factor(weight) for a zone to default (1.0).
    """
    try:
        query = text("UPDATE ways SET safety_factor = 1.0 WHERE gid = :id")
        await postgisDb.execute(query, {"id": zone_id})
        await postgisDb.commit()

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

        return {"status": "reset"}
    except Exception as e:
        await postgisDb.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/route")
async def get_route(
    request: RouteRequest,
    db: AsyncSession = Depends(get_db),
    postgisDb: AsyncSession = Depends(get_postgis_db),
    algorithm: str = Query(
        "dijkstra", description="Routing algorithm: ch, dijkstra, astar, bd_dijkstra"
    ),
):
    """
    Calculate route using pgRouting with safety weights.
    """
    try:
        # Metric
        SAFETY_SCORE_ROUTE_REQUESTS_TOTAL.inc()

        if algorithm == "ch":
            try:
                return await get_ch_route_geojson(request)
            except Exception:
                if not CH_FALLBACK_TO_DIJKSTRA:
                    raise
                algorithm = "dijkstra"

        # 1. Find nearest graph node by snapping to nearest edge endpoint.
        # This avoids scanning huge start/end-point candidate sets.
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

        start_res = await postgisDb.execute(
            node_query, {"lng": request.start.lng, "lat": request.start.lat}
        )
        start_node_res = start_res.fetchone()

        end_res = await postgisDb.execute(
            node_query, {"lng": request.end.lng, "lat": request.end.lat}
        )
        end_node_res = end_res.fetchone()

        if not start_node_res or not end_node_res:
            raise HTTPException(status_code=404, detail="Could not find nearest road nodes.")

        start_node = start_node_res[0]
        end_node = end_node_res[0]

        # 2. Build query against progressively larger local subgraphs, then full graph fallback.
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

        route_res = await postgisDb.execute(
            routing_query, {"sql": cost_sql, "start_node": start_node, "end_node": end_node}
        )
        routes = route_res.fetchall()
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
                route_res = await postgisDb.execute(
                    routing_query, {"sql": cost_sql, "start_node": start_node, "end_node": end_node}
                )
                routes = route_res.fetchall()
                if routes:
                    break
            except Exception:
                # If the local subgraph is too small/invalid, keep expanding.
                continue

        if not routes:
            # Fallback to full graph to avoid false negatives.
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

        # 4. Construct GeoJSON Response
        features = []
        total_distance = 0.0

        # Path Segments
        road_coords = []
        if ROUTE_DEBUG_LOG:
            print(f"--- Routing from {start_node} to {end_node} ---")
        for r in routes:
            if r.edge != -1:
                # print(f"Used Edge: {r.edge}, Cost: {r.cost}, Length: {r.length}")
                pass

            if r.geojson:
                geom = json.loads(r.geojson)
                coords = geom["coordinates"]

                # Check direction using Topology (Robust)
                if r.node == r.target:
                    # We are starting traversal from the Target node, so we are going backwards.
                    coords = coords[::-1]

                if not road_coords:
                    road_coords.extend(coords)
                else:
                    if road_coords[-1] == coords[0]:
                        road_coords.extend(coords[1:])
                    else:
                        road_coords.extend(coords)

                if r.length:
                    total_distance += r.length

        trimmed_coords = road_coords

        if trimmed_coords:
            # Start Connector
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "connector"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[request.start.lng, request.start.lat], trimmed_coords[0]],
                    },
                }
            )

            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "road"},
                    "geometry": {"type": "LineString", "coordinates": trimmed_coords},
                }
            )

            # End Connector
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "connector"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [trimmed_coords[-1], [request.end.lng, request.end.lat]],
                    },
                }
            )

        walking_speed_mps = 1.39
        duration_seconds = total_distance / walking_speed_mps

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

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

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph")
async def get_graph_geojson(
    min_lng: float = Query(..., description="Minimum Longitude(-180 ~ 180)"),
    min_lat: float = Query(..., description="Minimum Latitude(-90 ~ 90)"),
    max_lng: float = Query(..., description="Maximum Longitude(-180 ~ 180)"),
    max_lat: float = Query(..., description="Maximum Latitude(-90 ~ 90)"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(100, ge=1, le=2000, description="Items per page"),
    postgisDb: AsyncSession = Depends(get_postgis_db),
):
    """
    Return graph edges in bbox. Paginated. Response follows convention: data, filters, pagination.
    """
    try:
        filters_resp: Dict[str, Any] = {
            "min_lng": min_lng,
            "min_lat": min_lat,
            "max_lng": max_lng,
            "max_lat": max_lat,
        }
        params = {
            "min_lng": min_lng,
            "min_lat": min_lat,
            "max_lng": max_lng,
            "max_lat": max_lat,
        }

        # Total count in bbox
        count_query = text("""
            SELECT COUNT(*) FROM ways
            WHERE geometry && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
            """)
        count_result = await postgisDb.execute(count_query, params)
        total = count_result.scalar() or 0

        offset = (page - 1) * page_size
        params["limit"] = page_size
        params["offset"] = offset
        query = text("""
            SELECT gid, source, target, ST_AsGeoJSON(geometry) as geojson, safety_factor
            FROM ways
            WHERE geometry && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
            LIMIT 2000
            ORDER BY gid
            LIMIT :limit OFFSET :offset
        """
        )
        result = await postgisDb.execute(query, params)
        rows = result.fetchall()

        features = []
        for r in rows:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "edge", "id": r.gid, "weight": r.safety_factor},
                    "geometry": json.loads(r.geojson),
                }
            )

        pagination = PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        )
        return {
            "type": "FeatureCollection",
            "data": features,
            "features": features,  # backward compat
            "filters": filters_resp,
            "pagination": pagination.model_dump(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Existing Validated Endpoints (Keep for backward compatibility) ---


@app.get("/v1/safety/factors", response_model=SafetyFactorsResponse)
async def get_factors(body: SafetyFactorsRequest):
    # Business metric: count factors queries
    SAFETY_FACTORS_QUERIES_TOTAL.inc()

    return SafetyFactorsResponse(
        location=PointModel(lat=body.lat, lon=body.lon),
        radius_m=body.radius_m,
        factors={"cctv_cameras": 3, "street_lights": 5, "foot_traffic_level": "medium"},
        composite_score=88.0,
        queried_at=datetime.utcnow(),
    )


@app.post("/v1/safety/score-route", response_model=ScoreRouteResponse)
async def score_route(body: ScoreRouteRequest):
    # Business metric: count scoring requests
    SAFETY_SCORE_ROUTE_REQUESTS_TOTAL.inc()

    segs = [
        SafetySegmentScore(
            segment_id=f"seg_{i + 1:03d}",
            start_lat=s.start_lat,
            start_lon=s.start_lon,
            end_lat=s.end_lat,
            end_lon=s.end_lon,
            score=85 + i,
        )
        for i, s in enumerate(body.segments)
    ]
    return ScoreRouteResponse(
        overall_score=87.5,
        scoring_breakdown={
            "cctv_coverage": 90,
            "street_lighting": 85,
            "crime_rate": 82,
        },
        segments=segs,
        alerts=[],
        calculated_at=datetime.utcnow(),
    )


@app.put("/v1/safety/weights", response_model=SafetyWeightsResponse)
async def update_weights(body: SafetyWeightsRequest):
    w = body.weights
    total = (
        w.cctv_coverage
        + w.street_lighting
        + w.business_activity
        + w.crime_rate
        + w.pedestrian_traffic
    )

    # Business metric: count weights updates
    SAFETY_WEIGHTS_UPDATES_TOTAL.inc()

    return SafetyWeightsResponse(
        status="updated",
        user_id=body.user_id,
        weights=w,
        weights_sum=total,
        updated_at=datetime.utcnow(),
    )
