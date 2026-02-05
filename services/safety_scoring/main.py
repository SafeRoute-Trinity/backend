# Run:
# uvicorn services.safety_scoring.main:app --host 0.0.0.0 --port 20003 --reload
# Docs: http://127.0.0.1:20003/docs

import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.db import get_db
from libs.fastapi_service import ServiceAppConfig

app = FastAPI()

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
    weight: float


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
async def get_danger_zones(db: AsyncSession = Depends(get_db)):
    """
    Return edges that have custom weights.
    """
    try:
        query = text(
            """
            SELECT gid, safety_factor, ST_AsGeoJSON(geometry) as geojson 
            FROM ways 
            WHERE safety_factor != 1.0
        """
        )
        result = await db.execute(query)
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

        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        print(f"Error fetching zones: {e}")
        return {"type": "FeatureCollection", "features": []}


@app.post("/api/danger_zones")
async def update_danger_zone(update: WeightUpdateRequest, db: AsyncSession = Depends(get_db)):
    """
    Update safety weight for a specific edge and its bidirectional counterpart.
    """
    try:
        # Find the geometry of the selected edge
        geom_query = text("SELECT geometry FROM ways WHERE gid = :id")
        result = await db.execute(geom_query, {"id": update.edge_id})
        geom_res = result.fetchone()

        if not geom_res:
            raise HTTPException(status_code=404, detail="Edge not found")

        # Update ALL edges that share exactly the same geometry (spatial equality)
        update_query = text(
            """
            UPDATE ways 
            SET safety_factor = :w 
            WHERE ST_Equals(geometry, :geom)
        """
        )

        await db.execute(update_query, {"w": update.weight, "geom": geom_res[0]})
        await db.commit()

        # Metric
        SAFETY_WEIGHTS_UPDATES_TOTAL.inc()

        return {
            "status": "updated",
            "id": update.edge_id,
            "weight": update.weight,
            "note": "Updated bidirectional edges",
        }
    except Exception as e:
        await db.rollback()
        print(f"Error updating weight: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/danger_zones/{zone_id}")
async def reset_danger_zone(zone_id: int, db: AsyncSession = Depends(get_db)):
    """
    Reset safety info for a zone to default (1.0).
    """
    try:
        query = text("UPDATE ways SET safety_factor = 1.0 WHERE gid = :id")
        await db.execute(query, {"id": zone_id})
        await db.commit()
        return {"status": "reset"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/route")
async def get_route(request: RouteRequest, db: AsyncSession = Depends(get_db)):
    """
    Calculate route using pgRouting (Dijkstra) with safety weights.
    """
    try:
        # Metric
        SAFETY_SCORE_ROUTE_REQUESTS_TOTAL.inc()

        # 1. Find Nearest Node (Source or Target)
        node_query = text(
            """
        WITH node_candidates AS (
            SELECT source as id, ST_StartPoint(geometry) as geom FROM ways
            WHERE geometry && ST_Buffer(ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), 0.01)
            UNION ALL
            SELECT target as id, ST_EndPoint(geometry) as geom FROM ways
            WHERE geometry && ST_Buffer(ST_SetSRID(ST_MakePoint(:lng, :lat), 4326), 0.01)
        )
        SELECT id
        FROM node_candidates
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)
        LIMIT 1;
        """
        )

        start_res = await db.execute(
            node_query, {"lng": request.start.lng, "lat": request.start.lat}
        )
        start_node_res = start_res.fetchone()

        end_res = await db.execute(node_query, {"lng": request.end.lng, "lat": request.end.lat})
        end_node_res = end_res.fetchone()

        if not start_node_res or not end_node_res:
            raise HTTPException(status_code=404, detail="Could not find nearest road nodes.")

        start_node = start_node_res[0]
        end_node = end_node_res[0]

        # 2. Simplified Cost Query
        cost_sql = """
            SELECT 
                gid as id, 
                source, 
                target, 
                length * safety_factor as cost, 
                length * safety_factor as reverse_cost 
            FROM ways
        """

        # 3. Execute pgRouting
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
            FROM pgr_dijkstra(
                CAST(:sql AS TEXT),
                CAST(:start_node AS BIGINT), 
                CAST(:end_node AS BIGINT), 
                false
            ) as d
            LEFT JOIN ways w ON d.edge = w.gid
            ORDER BY d.seq;
        """
        )

        route_res = await db.execute(
            routing_query, {"sql": cost_sql, "start_node": start_node, "end_node": end_node}
        )
        routes = route_res.fetchall()

        if not routes:
            raise HTTPException(status_code=404, detail="No path found.")

        # 4. Construct GeoJSON Response
        features = []
        total_distance = 0.0

        # Path Segments
        road_coords = []
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

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph")
async def get_graph_geojson(
    min_lng: float = Query(..., description="Minimum Longitude"),
    min_lat: float = Query(..., description="Minimum Latitude"),
    max_lng: float = Query(..., description="Maximum Longitude"),
    max_lat: float = Query(..., description="Maximum Latitude"),
    db: AsyncSession = Depends(get_db),
):
    try:
        # Filter by bounding box using PostGIS && operator (overlap)
        # Limit to 2000 to prevent overload if zoomed out too far
        query = text(
            """
            SELECT gid, source, target, ST_AsGeoJSON(geometry) as geojson, safety_factor 
            FROM ways 
            WHERE geometry && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
            LIMIT 2000
        """
        )

        result = await db.execute(
            query, {"min_lng": min_lng, "min_lat": min_lat, "max_lng": max_lng, "max_lat": max_lat}
        )
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

        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Existing Validated Endpoints (Keep for backward compatibility) ---


@app.post("/v1/safety/score-route", response_model=ScoreRouteResponse)
async def score_route(body: ScoreRouteRequest):
    # Business metric: count scoring requests
    SAFETY_SCORE_ROUTE_REQUESTS_TOTAL.inc()

    segs = [
        SafetySegmentScore(
            segment_id=f"seg_{i+1:03d}",
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


@app.post("/v1/safety/factors", response_model=SafetyFactorsResponse)
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
