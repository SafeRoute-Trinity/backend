# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import time
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, Request, Response
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

from libs.postgis_db import get_postgis_db

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
    return {"status": "ok", "service": "routing_service"}


# @app.post("/v1/routes/calculate", response_model=RouteCalculateResponse)
# async def calc(body: RouteCalculateRequest):
#     # Business metric: initial route calculation
#     ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()


#     rid = f"rt_{uuid.uuid4().hex[:6]}"
#     now = datetime.utcnow()
#     opt = RouteOption(
#         route_index=0,
#         is_primary=True,
#         geometry="encoded_polyline_demo",
#         distance_m=2450,
#         duration_s=1800,
#         safety_score=87.5,
#         waypoints=[
#             Waypoint(lat=body.origin.lat, lon=body.origin.lon, instruction="Start"),
#             Waypoint(
#                 lat=body.destination.lat, lon=body.destination.lon, instruction="Arrive"
#             ),
#         ],
#     )
#     ROUTES[rid] = {
#         "route_id": rid,
#         "routes": [opt],
#         "alternatives_count": 1,
#         "calculated_at": now,
#     }
#     return RouteCalculateResponse(**ROUTES[rid])
@app.post("/v1/routes/calculate", response_model=RouteCalculateResponse)
async def calc(
    body: RouteCalculateRequest,
    db: AsyncSession = Depends(get_postgis_db),
):
    """
    简化版：用起点和终点生成一条直线 LINESTRING，
    存到 PostGIS 的 routes 表，再从数据库读回来组装响应。
    """
    rid = f"rt_{uuid.uuid4().hex[:6]}"
    # now = datetime.utcnow()

    # 注意：PostGIS 通常使用 (lon, lat) 顺序
    origin_lon, origin_lat = body.origin.lon, body.origin.lat
    dest_lon, dest_lat = body.destination.lon, body.destination.lat

    # 构造 WKT：LINESTRING(lon lat, lon lat)
    wkt = f"LINESTRING({origin_lon} {origin_lat}, {dest_lon} {dest_lat})"

    # 简单估一个时长（这里还是用你之前的固定值 / 或者以后自己算）
    duration_s = 1800
    safety_score = 87.5

    # 用 PostGIS 插入一条记录，顺便算一下大圆距离（单位：米）
    insert_sql = text(
        """
        INSERT INTO routes (route_id, user_id, geom, distance_m, duration_s, safety_score)
        VALUES (
            :route_id,
            NULL,
            ST_GeomFromText(:wkt, 4326),
            ST_DistanceSphere(
                ST_MakePoint(:origin_lon, :origin_lat),
                ST_MakePoint(:dest_lon, :dest_lat)
            ),
            :duration_s,
            :safety_score
        )
        RETURNING
            route_id,
            ST_AsText(geom) AS wkt_geom,
            distance_m,
            duration_s,
            safety_score,
            created_at;
        """
    )

    result = await db.execute(
        insert_sql,
        {
            "route_id": rid,
            "wkt": wkt,
            "origin_lon": origin_lon,
            "origin_lat": origin_lat,
            "dest_lon": dest_lon,
            "dest_lat": dest_lat,
            "duration_s": duration_s,
            "safety_score": safety_score,
        },
    )
    await db.commit()

    row = result.mappings().one()

    # 用数据库里的结果构造 RouteOption / RouteCalculateResponse
    opt = RouteOption(
        route_index=0,
        is_primary=True,
        geometry=row[
            "wkt_geom"
        ],  # 这里我们直接返回 WKT，你以后可以换成 polyline/GeoJSON
        distance_m=int(row["distance_m"]) if row["distance_m"] is not None else 0,
        duration_s=row["duration_s"],
        safety_score=row["safety_score"],
        waypoints=[
            Waypoint(lat=body.origin.lat, lon=body.origin.lon, instruction="Start"),
            Waypoint(
                lat=body.destination.lat,
                lon=body.destination.lon,
                instruction="Arrive",
            ),
        ],
    )

    return RouteCalculateResponse(
        route_id=row["route_id"],
        routes=[opt],
        alternatives_count=1,
        calculated_at=row["created_at"],
    )


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
