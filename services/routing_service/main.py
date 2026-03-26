# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
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
# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from models.audit import Audit
from libs.cas_logger import Op, cas_log
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
    from .csa import DEFAULT_CSA_SCHEDULE_PATH, load_csa_schedule, plan_journey_with_csa
    from .openrouteservice_client import get_ors_client
except ImportError:
    # Fall back to absolute imports (when run directly)
    from csa import DEFAULT_CSA_SCHEDULE_PATH, load_csa_schedule, plan_journey_with_csa
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
ROUTE_ENGINE = os.getenv("ROUTE_ENGINE", "pgrouting").strip().lower()
CH_ROUTING_SERVICE_URL = os.getenv(
    "CH_ROUTING_SERVICE_URL",
    os.getenv("GRAPHHOPPER_PROXY_SERVICE_URL", "http://127.0.0.1:20007"),
).rstrip("/")
CH_ROUTING_TIMEOUT_SECONDS = float(os.getenv("CH_ROUTING_TIMEOUT_SECONDS", "8"))
CH_FALLBACK_TO_DIJKSTRA = os.getenv("CH_FALLBACK_TO_DIJKSTRA", "true").lower() == "true"
CH_SUPPORTS_DYNAMIC_WEIGHTS = (
    os.getenv("CH_SUPPORTS_DYNAMIC_WEIGHTS", "false").strip().lower() == "true"
)
CH_ROUTING_PROFILE_WEIGHTED = os.getenv(
    "CH_ROUTING_PROFILE_WEIGHTED",
    os.getenv("GRAPHHOPPER_PROFILE", "foot"),
)
CH_ROUTING_PROFILE_FAST = os.getenv(
    "CH_ROUTING_PROFILE_FAST",
    CH_ROUTING_PROFILE_WEIGHTED,
)
TRANSIT_CACHE_ENABLED = os.getenv("TRANSIT_CACHE_ENABLED", "true").lower() == "true"
TRANSIT_CACHE_TTL_SECONDS = int(os.getenv("TRANSIT_CACHE_TTL_SECONDS", "900"))
TRANSIT_CACHE_BUCKET_MINUTES = int(os.getenv("TRANSIT_CACHE_BUCKET_MINUTES", "5"))
TRANSIT_CACHE_TABLE = "saferoute.transit_plan_cache"
TRANSIT_CACHE_TABLE_INITIALIZED = False
ROUTE_SAFETY_FACTOR_MIN = float(os.getenv("ROUTE_SAFETY_FACTOR_MIN", "0.5"))
ROUTE_SAFETY_FACTOR_MAX = float(os.getenv("ROUTE_SAFETY_FACTOR_MAX", "50"))
ROUTE_SAFETY_FACTOR_EXPONENT = float(os.getenv("ROUTE_SAFETY_FACTOR_EXPONENT", "0.65"))
ROUTE_MAX_DETOUR_RATIO = float(os.getenv("ROUTE_MAX_DETOUR_RATIO", "1.45"))

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


class TransitPlanRequest(BaseModel):
    origin: Point
    destination: Point
    departure_time: Optional[datetime] = None
    max_walking_distance_m: float = Field(
        1200.0,
        ge=100.0,
        le=5000.0,
        description="Maximum walking distance allowed for access/egress.",
    )
    max_transfers: int = Field(4, ge=0, le=8)
    search_window_minutes: int = Field(180, ge=15, le=720)


class TransitLegResponse(BaseModel):
    mode: Literal["walk", "transit"]
    from_stop_id: Optional[str] = None
    to_stop_id: Optional[str] = None
    route_id: Optional[str] = None
    trip_id: Optional[str] = None
    vehicle_type: Optional[str] = None
    departure_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None
    duration_s: int
    distance_m: Optional[float] = None
    coordinates: Optional[List[List[float]]] = None


class TransitItineraryResponse(BaseModel):
    departure_time: datetime
    arrival_time: datetime
    duration_s: int
    transfers: int
    walking_duration_s: int
    transit_duration_s: int
    legs: List[TransitLegResponse]


class TransitPlanResponse(BaseModel):
    algorithm: Literal["csa", "google_transit"] = "csa"
    generated_at: datetime
    schedule_version: str
    schedule_path: str
    itineraries: List[TransitItineraryResponse]


def _coerce_utc_datetime(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_google_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_google_duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str) and value.endswith("s"):
        try:
            return max(0, int(float(value[:-1])))
        except ValueError:
            return 0
    return 0


def _decode_google_polyline(encoded: str, precision: int = 5) -> List[List[float]]:
    """Decode Google encoded polyline into [lon, lat] coordinates."""
    if not encoded:
        return []

    index = 0
    lat = 0
    lon = 0
    coordinates: List[List[float]] = []
    factor = 10**precision

    while index < len(encoded):
        shift = 0
        result = 0
        while True:
            if index >= len(encoded):
                return coordinates
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        delta_lat = ~(result >> 1) if (result & 1) else (result >> 1)
        lat += delta_lat

        shift = 0
        result = 0
        while True:
            if index >= len(encoded):
                return coordinates
            byte = ord(encoded[index]) - 63
            index += 1
            result |= (byte & 0x1F) << shift
            shift += 5
            if byte < 0x20:
                break
        delta_lon = ~(result >> 1) if (result & 1) else (result >> 1)
        lon += delta_lon

        coordinates.append([lon / factor, lat / factor])

    return coordinates


def _extract_step_coordinates(step: Dict[str, Any]) -> Optional[List[List[float]]]:
    polyline = step.get("polyline") or {}
    encoded = polyline.get("encodedPolyline")
    if not isinstance(encoded, str) or not encoded:
        return None
    decoded = _decode_google_polyline(encoded)
    return decoded if len(decoded) >= 2 else None


def _extract_google_stop_lon_lat(stop_payload: Any) -> Optional[List[float]]:
    if not isinstance(stop_payload, dict):
        return None

    lat_lng = stop_payload.get("location", {}).get("latLng")
    if isinstance(lat_lng, dict):
        lat = lat_lng.get("latitude")
        lon = lat_lng.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return [float(lon), float(lat)]

    direct_lat = stop_payload.get("lat")
    direct_lon = stop_payload.get("lon")
    if isinstance(direct_lat, (int, float)) and isinstance(direct_lon, (int, float)):
        return [float(direct_lon), float(direct_lat)]

    return None


def _is_valid_lon_lat_pair(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    )


def _coerce_lon_lat_pair(value: Any) -> Optional[List[float]]:
    if _is_valid_lon_lat_pair(value):
        return [float(value[0]), float(value[1])]

    if isinstance(value, dict):
        lon = value.get("lon", value.get("longitude"))
        lat = value.get("lat", value.get("latitude"))
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            return [float(lon), float(lat)]

    return None


def _extract_leg_bound_coordinates(
    leg: Dict[str, Any],
) -> tuple[Optional[List[float]], Optional[List[float]]]:
    coordinates = leg.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None, None

    first = coordinates[0]
    last = coordinates[-1]
    start = [float(first[0]), float(first[1])] if _is_valid_lon_lat_pair(first) else None
    end = [float(last[0]), float(last[1])] if _is_valid_lon_lat_pair(last) else None
    return start, end


def _extract_coordinates_from_weighted_geojson(route_geojson: Dict[str, Any]) -> List[List[float]]:
    merged: List[List[float]] = []
    features = route_geojson.get("features")
    if not isinstance(features, list):
        return merged

    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if isinstance(properties, dict) and str(properties.get("type", "")).lower() == "connector":
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
            continue

        coords_raw = geometry.get("coordinates")
        if not isinstance(coords_raw, list):
            continue

        normalized: List[List[float]] = []
        for coord in coords_raw:
            if _is_valid_lon_lat_pair(coord):
                normalized.append([float(coord[0]), float(coord[1])])

        if len(normalized) < 2:
            continue

        if merged and merged[-1] == normalized[0]:
            merged.extend(normalized[1:])
        else:
            merged.extend(normalized)

    return merged


def _extract_leg_stop_coord(leg: Dict[str, Any], key: str) -> Optional[List[float]]:
    if not isinstance(leg, dict):
        return None
    return _coerce_lon_lat_pair(leg.get(key))


def _infer_walk_leg_endpoints(
    legs: List[Dict[str, Any]],
    leg_index: int,
    origin: Point,
    destination: Point,
    stop_coords_by_id: Optional[Dict[str, List[float]]] = None,
) -> tuple[Optional[List[float]], Optional[List[float]]]:
    leg = legs[leg_index]
    start_coord, end_coord = _extract_leg_bound_coordinates(leg)

    from_stop_id = leg.get("from_stop_id")
    to_stop_id = leg.get("to_stop_id")

    if start_coord is None:
        start_coord = _extract_leg_stop_coord(leg, "from_stop_coord")
    if end_coord is None:
        end_coord = _extract_leg_stop_coord(leg, "to_stop_coord")

    if start_coord is None and isinstance(from_stop_id, str) and from_stop_id and stop_coords_by_id:
        start_coord = stop_coords_by_id.get(from_stop_id)
    if end_coord is None and isinstance(to_stop_id, str) and to_stop_id and stop_coords_by_id:
        end_coord = stop_coords_by_id.get(to_stop_id)

    prev_leg = legs[leg_index - 1] if leg_index > 0 else None
    next_leg = legs[leg_index + 1] if leg_index < len(legs) - 1 else None

    if start_coord is None and isinstance(prev_leg, dict):
        start_coord = _extract_leg_stop_coord(prev_leg, "to_stop_coord")
    if end_coord is None and isinstance(next_leg, dict):
        end_coord = _extract_leg_stop_coord(next_leg, "from_stop_coord")

    if start_coord is None and isinstance(prev_leg, dict):
        _, prev_end = _extract_leg_bound_coordinates(prev_leg)
        start_coord = prev_end
    if end_coord is None and isinstance(next_leg, dict):
        next_start, _ = _extract_leg_bound_coordinates(next_leg)
        end_coord = next_start

    if start_coord is None and from_stop_id is None and leg_index == 0:
        start_coord = [origin.lon, origin.lat]
    if end_coord is None and to_stop_id is None and leg_index == len(legs) - 1:
        end_coord = [destination.lon, destination.lat]

    return start_coord, end_coord


async def _enrich_walk_legs_with_weighted_routes(
    legs: List[Dict[str, Any]],
    origin: Point,
    destination: Point,
    db: AsyncSession,
    stop_coords_by_id: Optional[Dict[str, List[float]]] = None,
) -> List[Dict[str, Any]]:
    enriched_legs = [dict(leg) for leg in legs]

    for index, leg in enumerate(enriched_legs):
        if leg.get("mode") != "walk":
            continue

        start_coord, end_coord = _infer_walk_leg_endpoints(
            legs=enriched_legs,
            leg_index=index,
            origin=origin,
            destination=destination,
            stop_coords_by_id=stop_coords_by_id,
        )
        if not start_coord or not end_coord:
            continue

        # Skip degenerate segments.
        if start_coord == end_coord:
            continue

        weighted_request = RouteRequest(
            start=Coordinate(lat=float(start_coord[1]), lng=float(start_coord[0])),
            end=Coordinate(lat=float(end_coord[1]), lng=float(end_coord[0])),
        )

        try:
            weighted_geojson = await _compute_weighted_route_geojson(
                request=weighted_request,
                db=db,
                algorithm="dijkstra",
            )
            weighted_coordinates = _extract_coordinates_from_weighted_geojson(weighted_geojson)
            if len(weighted_coordinates) < 2:
                continue

            leg["coordinates"] = weighted_coordinates
            summary = (weighted_geojson.get("properties") or {}).get("summary") or {}
            weighted_distance = summary.get("distance_meters")
            if isinstance(weighted_distance, (int, float)) and weighted_distance > 0:
                leg["distance_m"] = round(float(weighted_distance), 1)
        except Exception as e:
            logger.warning(
                "Transit walk leg weighted-route enrichment failed on leg %s: %s", index, e
            )

    return enriched_legs


async def _build_weighted_transit_itinerary(
    itinerary_payload: Dict[str, Any],
    body: TransitPlanRequest,
    db: AsyncSession,
    stop_coords_by_id: Optional[Dict[str, List[float]]] = None,
) -> TransitItineraryResponse:
    payload = dict(itinerary_payload)
    legs_payload = payload.get("legs") or []
    if not isinstance(legs_payload, list):
        legs_payload = []
    payload["legs"] = await _enrich_walk_legs_with_weighted_routes(
        legs=legs_payload,
        origin=body.origin,
        destination=body.destination,
        db=db,
        stop_coords_by_id=stop_coords_by_id,
    )
    payload["legs"] = [TransitLegResponse(**leg) for leg in payload["legs"]]
    return TransitItineraryResponse(**payload)


def _normalize_transit_vehicle_type(raw_type: Any) -> Optional[str]:
    if not isinstance(raw_type, str):
        return None

    value = raw_type.strip().upper()
    if not value:
        return None

    if value in {"BUS", "TROLLEYBUS", "INTERCITY_BUS", "SHARE_TAXI"}:
        return "BUS"
    if value in {"TRAM", "LIGHT_RAIL"}:
        return "TRAM"
    if value in {
        "SUBWAY",
        "METRO_RAIL",
        "HEAVY_RAIL",
        "COMMUTER_TRAIN",
        "HIGH_SPEED_TRAIN",
        "LONG_DISTANCE_TRAIN",
        "RAIL",
        "TRAIN",
        "MONORAIL",
    }:
        return "RAIL"
    if value == "FERRY":
        return "FERRY"

    return value


def _round_coord(value: float) -> float:
    return round(float(value), 5)


def _bucket_departure_time(value: datetime) -> datetime:
    bucket_minutes = max(1, TRANSIT_CACHE_BUCKET_MINUTES)
    departure_utc = _coerce_utc_datetime(value)
    rounded_minute = (departure_utc.minute // bucket_minutes) * bucket_minutes
    return departure_utc.replace(minute=rounded_minute, second=0, microsecond=0)


def _build_transit_cache_key(
    body: TransitPlanRequest, provider: str, departure_time: datetime
) -> tuple[str, datetime]:
    departure_bucket = _bucket_departure_time(departure_time)
    cache_schema_version = os.getenv("TRANSIT_CACHE_SCHEMA_VERSION", "v4-google-walk-merged")
    routing_preference = (
        os.getenv("GOOGLE_TRANSIT_ROUTING_PREFERENCE", "LESS_WALKING")
        if provider == "google"
        else ""
    )
    key_source = "|".join(
        [
            cache_schema_version,
            provider,
            f"{_round_coord(body.origin.lat):.5f}",
            f"{_round_coord(body.origin.lon):.5f}",
            f"{_round_coord(body.destination.lat):.5f}",
            f"{_round_coord(body.destination.lon):.5f}",
            departure_bucket.isoformat(),
            str(int(body.max_walking_distance_m)),
            str(body.max_transfers),
            str(body.search_window_minutes),
            routing_preference,
        ]
    )
    return sha256(key_source.encode("utf-8")).hexdigest(), departure_bucket


async def _ensure_transit_cache_table(db: AsyncSession) -> None:
    global TRANSIT_CACHE_TABLE_INITIALIZED
    if TRANSIT_CACHE_TABLE_INITIALIZED:
        return

    await db.execute(text("CREATE SCHEMA IF NOT EXISTS saferoute"))
    await db.execute(text("""
            CREATE TABLE IF NOT EXISTS saferoute.transit_plan_cache (
                cache_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                origin_lat DOUBLE PRECISION NOT NULL,
                origin_lon DOUBLE PRECISION NOT NULL,
                destination_lat DOUBLE PRECISION NOT NULL,
                destination_lon DOUBLE PRECISION NOT NULL,
                departure_bucket TIMESTAMPTZ NOT NULL,
                request_payload JSONB NOT NULL,
                response_payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at TIMESTAMPTZ
            )
            """))
    await db.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_transit_plan_cache_expires_at
            ON saferoute.transit_plan_cache (expires_at)
            """))
    await db.commit()
    TRANSIT_CACHE_TABLE_INITIALIZED = True


async def _get_cached_transit_itinerary(
    db: AsyncSession, cache_key: str
) -> Optional[Dict[str, Any]]:
    if not TRANSIT_CACHE_ENABLED:
        return None

    try:
        await _ensure_transit_cache_table(db)
        result = await db.execute(
            text(f"""
                SELECT response_payload
                FROM {TRANSIT_CACHE_TABLE}
                WHERE cache_key = :cache_key
                  AND expires_at > NOW()
                LIMIT 1
                """),
            {"cache_key": cache_key},
        )
        row = result.first()
        if not row:
            return None

        payload = row.response_payload
        await db.execute(
            text(f"""
                UPDATE {TRANSIT_CACHE_TABLE}
                SET hit_count = hit_count + 1,
                    last_hit_at = NOW(),
                    updated_at = NOW()
                WHERE cache_key = :cache_key
                """),
            {"cache_key": cache_key},
        )
        await db.commit()

        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            return payload
    except Exception as e:
        logger.warning("Transit cache read failed, fallback to provider call: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass

    return None


async def _store_transit_itinerary_cache(
    db: AsyncSession,
    cache_key: str,
    provider: str,
    body: TransitPlanRequest,
    departure_bucket: datetime,
    itinerary_payload: Dict[str, Any],
) -> None:
    if not TRANSIT_CACHE_ENABLED:
        return

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, TRANSIT_CACHE_TTL_SECONDS))
    request_payload = {
        "origin": {"lat": body.origin.lat, "lon": body.origin.lon},
        "destination": {"lat": body.destination.lat, "lon": body.destination.lon},
        "departure_bucket": departure_bucket.isoformat(),
        "max_walking_distance_m": body.max_walking_distance_m,
        "max_transfers": body.max_transfers,
        "search_window_minutes": body.search_window_minutes,
    }

    try:
        await _ensure_transit_cache_table(db)
        await db.execute(
            text(f"""
                INSERT INTO {TRANSIT_CACHE_TABLE} (
                    cache_key,
                    provider,
                    origin_lat,
                    origin_lon,
                    destination_lat,
                    destination_lon,
                    departure_bucket,
                    request_payload,
                    response_payload,
                    expires_at
                )
                VALUES (
                    :cache_key,
                    :provider,
                    :origin_lat,
                    :origin_lon,
                    :destination_lat,
                    :destination_lon,
                    :departure_bucket,
                    CAST(:request_payload AS JSONB),
                    CAST(:response_payload AS JSONB),
                    :expires_at
                )
                ON CONFLICT (cache_key)
                DO UPDATE SET
                    provider = EXCLUDED.provider,
                    origin_lat = EXCLUDED.origin_lat,
                    origin_lon = EXCLUDED.origin_lon,
                    destination_lat = EXCLUDED.destination_lat,
                    destination_lon = EXCLUDED.destination_lon,
                    departure_bucket = EXCLUDED.departure_bucket,
                    request_payload = EXCLUDED.request_payload,
                    response_payload = EXCLUDED.response_payload,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                """),
            {
                "cache_key": cache_key,
                "provider": provider,
                "origin_lat": body.origin.lat,
                "origin_lon": body.origin.lon,
                "destination_lat": body.destination.lat,
                "destination_lon": body.destination.lon,
                "departure_bucket": departure_bucket,
                "request_payload": json.dumps(request_payload),
                "response_payload": json.dumps(itinerary_payload),
                "expires_at": expires_at,
            },
        )
        await db.commit()
    except Exception as e:
        logger.warning("Transit cache write failed, continuing without cache persistence: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass


async def _plan_transit_with_google(
    body: TransitPlanRequest, departure_time: datetime
) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY is not configured")

    endpoint = os.getenv(
        "GOOGLE_TRANSIT_ROUTES_URL",
        "https://routes.googleapis.com/directions/v2:computeRoutes",
    )
    departure_utc = _coerce_utc_datetime(departure_time)
    routing_preference = os.getenv("GOOGLE_TRANSIT_ROUTING_PREFERENCE", "LESS_WALKING")
    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": body.origin.lat,
                    "longitude": body.origin.lon,
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": body.destination.lat,
                    "longitude": body.destination.lon,
                }
            }
        },
        "travelMode": "TRANSIT",
        "departureTime": departure_utc.isoformat().replace("+00:00", "Z"),
        "computeAlternativeRoutes": False,
        "transitPreferences": {
            "routingPreference": routing_preference,
        },
    }
    field_mask = ",".join(
        [
            "routes.duration",
            "routes.distanceMeters",
            "routes.legs.duration",
            "routes.legs.distanceMeters",
            "routes.legs.steps.travelMode",
            "routes.legs.steps.staticDuration",
            "routes.legs.steps.distanceMeters",
            "routes.legs.steps.polyline",
            "routes.legs.steps.transitDetails",
        ]
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": field_mask,
                },
                json=payload,
            )
    except Exception as e:
        logger.exception("Failed to call Google Routes transit endpoint.")
        raise HTTPException(status_code=502, detail=f"Google transit request failed: {e}") from e

    if response.status_code != 200:
        message = f"Google transit endpoint returned {response.status_code}"
        try:
            error_detail = response.json().get("error", {}).get("message")
            if error_detail:
                message = error_detail
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Google transit error: {message}",
        )

    payload = response.json()
    routes = payload.get("routes") or []
    if not routes:
        return None
    primary_route = routes[0]
    route_legs = primary_route.get("legs") or []
    if not route_legs:
        return None
    leg = route_legs[0]

    steps = leg.get("steps") or []
    total_duration_s = _parse_google_duration_seconds(
        leg.get("duration")
    ) or _parse_google_duration_seconds(primary_route.get("duration"))
    itinerary_departure = departure_utc

    legs_payload: List[Dict[str, Any]] = []
    walking_duration_s = 0
    transit_duration_s = 0
    timeline_cursor = itinerary_departure
    pending_walk: Optional[Dict[str, Any]] = None

    def _merge_walk_coordinates(
        existing_coords: Optional[List[List[float]]], new_coords: Optional[List[List[float]]]
    ) -> Optional[List[List[float]]]:
        if not isinstance(new_coords, list) or len(new_coords) < 2:
            return existing_coords

        normalized_new: List[List[float]] = []
        for coord in new_coords:
            if _is_valid_lon_lat_pair(coord):
                normalized_new.append([float(coord[0]), float(coord[1])])

        if len(normalized_new) < 2:
            return existing_coords

        if not isinstance(existing_coords, list) or len(existing_coords) == 0:
            return normalized_new

        merged = list(existing_coords)
        if merged[-1] == normalized_new[0]:
            merged.extend(normalized_new[1:])
        else:
            merged.extend(normalized_new)
        return merged

    def _flush_pending_walk() -> None:
        nonlocal pending_walk, walking_duration_s
        if pending_walk is None:
            return

        coordinates = pending_walk.get("coordinates")
        if isinstance(coordinates, list):
            compacted: List[List[float]] = []
            for coord in coordinates:
                if _is_valid_lon_lat_pair(coord):
                    normalized = [float(coord[0]), float(coord[1])]
                    if not compacted or compacted[-1] != normalized:
                        compacted.append(normalized)
            pending_walk["coordinates"] = compacted if len(compacted) >= 2 else None
        else:
            pending_walk["coordinates"] = None

        legs_payload.append(pending_walk)
        walking_duration_s += int(pending_walk.get("duration_s") or 0)
        pending_walk = None

    for step in steps:
        travel_mode = (step.get("travelMode") or "").upper()
        step_duration_s = _parse_google_duration_seconds(step.get("staticDuration"))
        step_distance_m = step.get("distanceMeters")
        step_coordinates = _extract_step_coordinates(step)

        if travel_mode == "TRANSIT":
            _flush_pending_walk()

            transit_details = step.get("transitDetails") or {}
            stop_details = transit_details.get("stopDetails") or {}
            line = transit_details.get("transitLine") or {}
            vehicle = line.get("vehicle") or {}
            departure_stop = stop_details.get("departureStop") or {}
            arrival_stop = stop_details.get("arrivalStop") or {}
            departure_stop_coord = _extract_google_stop_lon_lat(departure_stop)
            arrival_stop_coord = _extract_google_stop_lon_lat(arrival_stop)
            vehicle_type = _normalize_transit_vehicle_type(vehicle.get("type"))
            route_id = (
                line.get("nameShort")
                or line.get("name")
                or ((line.get("vehicle") or {}).get("name") or {}).get("text")
                or "Transit"
            )
            departure_dt = _parse_google_iso_datetime(stop_details.get("departureTime"))
            arrival_dt = _parse_google_iso_datetime(stop_details.get("arrivalTime"))
            if departure_dt is None:
                departure_dt = timeline_cursor
            if arrival_dt is None:
                arrival_dt = departure_dt + timedelta(seconds=step_duration_s)
            transit_leg_duration = max(
                step_duration_s,
                int((arrival_dt - departure_dt).total_seconds()),
            )
            legs_payload.append(
                {
                    "mode": "transit",
                    "from_stop_id": departure_stop.get("name"),
                    "to_stop_id": arrival_stop.get("name"),
                    "from_stop_coord": departure_stop_coord,
                    "to_stop_coord": arrival_stop_coord,
                    "route_id": route_id,
                    "trip_id": transit_details.get("headsign"),
                    "vehicle_type": vehicle_type,
                    "departure_time": departure_dt,
                    "arrival_time": arrival_dt,
                    "duration_s": transit_leg_duration,
                    "distance_m": float(step_distance_m) if step_distance_m is not None else None,
                    "coordinates": step_coordinates,
                }
            )
            transit_duration_s += transit_leg_duration
            timeline_cursor = max(timeline_cursor, arrival_dt)
        else:
            walk_departure = timeline_cursor
            walk_arrival = walk_departure + timedelta(seconds=step_duration_s)
            distance_value = (
                float(step_distance_m) if isinstance(step_distance_m, (int, float)) else None
            )

            if pending_walk is None:
                pending_walk = {
                    "mode": "walk",
                    "departure_time": walk_departure,
                    "arrival_time": walk_arrival,
                    "duration_s": step_duration_s,
                    "vehicle_type": None,
                    "distance_m": distance_value,
                    "coordinates": _merge_walk_coordinates(None, step_coordinates),
                }
            else:
                pending_walk["arrival_time"] = walk_arrival
                pending_walk["duration_s"] = (
                    int(pending_walk.get("duration_s") or 0) + step_duration_s
                )

                existing_distance = pending_walk.get("distance_m")
                if isinstance(existing_distance, (int, float)) and distance_value is not None:
                    pending_walk["distance_m"] = float(existing_distance) + distance_value
                elif existing_distance is None:
                    pending_walk["distance_m"] = distance_value

                pending_walk["coordinates"] = _merge_walk_coordinates(
                    pending_walk.get("coordinates"),
                    step_coordinates,
                )

            timeline_cursor = walk_arrival

    _flush_pending_walk()

    transit_legs_count = len([item for item in legs_payload if item["mode"] == "transit"])
    transfers = max(0, transit_legs_count - 1)
    itinerary_arrival = timeline_cursor
    computed_duration_s = int((itinerary_arrival - itinerary_departure).total_seconds())
    final_duration_s = max(
        1, total_duration_s or computed_duration_s or walking_duration_s + transit_duration_s
    )
    if total_duration_s and total_duration_s > computed_duration_s:
        itinerary_arrival = itinerary_departure + timedelta(seconds=total_duration_s)

    return {
        "departure_time": itinerary_departure,
        "arrival_time": itinerary_arrival,
        "duration_s": final_duration_s,
        "transfers": transfers,
        "walking_duration_s": walking_duration_s,
        "transit_duration_s": transit_duration_s,
        "legs": legs_payload,
    }


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


def _ch_profile_from_preferences(
    optimize_for: Literal["safety", "time", "distance", "balanced"],
) -> str:
    if optimize_for == "time":
        return CH_ROUTING_PROFILE_FAST
    return CH_ROUTING_PROFILE_WEIGHTED


async def _compute_ch_route_geojson(
    request: RouteRequest,
    optimize_for: Literal["safety", "time", "distance", "balanced"],
) -> Dict[str, Any]:
    profile = _ch_profile_from_preferences(optimize_for)
    url = f"{CH_ROUTING_SERVICE_URL}/api/route"
    payload = {
        "start": {"lat": request.start.lat, "lng": request.start.lng},
        "end": {"lat": request.end.lat, "lng": request.end.lng},
    }

    try:
        async with httpx.AsyncClient(timeout=CH_ROUTING_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                params={"algorithm": "ch", "profile": profile},
                json=payload,
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"CH routing request failed: {e}") from e

    if response.status_code >= 400:
        detail = response.text[:300] if response.text else response.reason_phrase
        raise HTTPException(
            status_code=502,
            detail=f"CH routing error ({response.status_code}): {detail}",
        )

    data = response.json()
    if data.get("type") != "FeatureCollection":
        raise HTTPException(status_code=502, detail="CH routing returned invalid GeoJSON payload")
    return data


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

    async def _query_routes(
        subgraph_cost_expr: str,
        fullgraph_cost_expr: str,
    ) -> List[Any]:
        queried_routes: List[Any] = []
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
                    {subgraph_cost_expr} AS cost,
                    {subgraph_cost_expr} AS reverse_cost,
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
                queried_routes = route_res.fetchall()
                if queried_routes:
                    break
            except Exception:
                continue

        if queried_routes:
            return queried_routes

        full_cost_sql = f"""
            SELECT
                gid AS id,
                source,
                target,
                {fullgraph_cost_expr} AS cost,
                {fullgraph_cost_expr} AS reverse_cost,
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
        return route_res.fetchall()

    sf_min = min(ROUTE_SAFETY_FACTOR_MIN, ROUTE_SAFETY_FACTOR_MAX)
    sf_max = max(ROUTE_SAFETY_FACTOR_MIN, ROUTE_SAFETY_FACTOR_MAX)
    sf_exp = max(0.01, ROUTE_SAFETY_FACTOR_EXPONENT)
    max_detour_ratio = max(1.0, ROUTE_MAX_DETOUR_RATIO)

    weighted_subgraph_cost_expr = (
        f"w.length * POWER(LEAST(GREATEST(w.safety_factor, {sf_min}), {sf_max}), {sf_exp})"
    )
    weighted_fullgraph_cost_expr = (
        f"length * POWER(LEAST(GREATEST(safety_factor, {sf_min}), {sf_max}), {sf_exp})"
    )
    routes = await _query_routes(
        subgraph_cost_expr=weighted_subgraph_cost_expr,
        fullgraph_cost_expr=weighted_fullgraph_cost_expr,
    )

    if not routes:
        raise HTTPException(status_code=404, detail="No path found.")

    def _distance_for_rows(route_rows: List[Any]) -> float:
        total = 0.0
        for route_row in route_rows:
            if route_row.length:
                total += float(route_row.length)
        return total

    selected_routes = routes
    route_strategy = "weighted_safety"
    shortest_distance: Optional[float] = None
    weighted_distance = _distance_for_rows(routes)
    detour_ratio: Optional[float] = None

    if max_detour_ratio > 1.0:
        try:
            shortest_routes = await _query_routes(
                subgraph_cost_expr="w.length",
                fullgraph_cost_expr="length",
            )
            shortest_distance = _distance_for_rows(shortest_routes)
            if shortest_distance > 0:
                detour_ratio = weighted_distance / shortest_distance
                if detour_ratio > max_detour_ratio:
                    selected_routes = shortest_routes
                    route_strategy = "detour_capped_shortest"
                    logger.info(
                        "Weighted route detour ratio %.3f exceeded max %.3f, using shortest path.",
                        detour_ratio,
                        max_detour_ratio,
                    )
        except Exception as e:
            logger.warning("Failed to compute shortest-path detour guardrail: %s", e)

    def _coord_distance_sq(a: List[float], b: List[float]) -> float:
        dx = float(a[0]) - float(b[0])
        dy = float(a[1]) - float(b[1])
        return dx * dx + dy * dy

    def _coord_distance_meters(a: List[float], b: List[float]) -> float:
        lon1, lat1 = math.radians(float(a[0])), math.radians(float(a[1]))
        lon2, lat2 = math.radians(float(b[0])), math.radians(float(b[1]))
        dlon = lon2 - lon1
        dlat = lat2 - lat1
        h = math.sin(dlat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
        return 2.0 * 6371000.0 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1.0 - h)))

    def _normalize_line_coordinates(raw_coords: Any) -> List[List[float]]:
        if not isinstance(raw_coords, list):
            return []
        normalized: List[List[float]] = []
        for coord in raw_coords:
            if _is_valid_lon_lat_pair(coord):
                normalized.append([float(coord[0]), float(coord[1])])
        return normalized

    def _append_continuous_segment(
        merged_path: List[List[float]],
        segment_coords: List[List[float]],
        start_hint: List[float],
    ) -> None:
        if len(segment_coords) < 2:
            return

        segment = list(segment_coords)
        if not merged_path:
            if _coord_distance_sq(start_hint, segment[-1]) < _coord_distance_sq(
                start_hint, segment[0]
            ):
                segment.reverse()
            merged_path.extend(segment)
            return

        if _coord_distance_sq(merged_path[-1], segment[-1]) < _coord_distance_sq(
            merged_path[-1], segment[0]
        ):
            segment.reverse()

        if _coord_distance_sq(merged_path[-1], segment[0]) <= 1e-12:
            merged_path.extend(segment[1:])
        else:
            merged_path.extend(segment)

    def _prune_backtrack_spikes(path_coords: List[List[float]]) -> List[List[float]]:
        if len(path_coords) < 3:
            return path_coords

        pruned: List[List[float]] = []
        for point in path_coords:
            pruned.append(point)
            # Remove local A->B->A spikes that render as dead-end branches.
            while len(pruned) >= 3 and _coord_distance_sq(pruned[-1], pruned[-3]) <= 1e-12:
                pruned.pop()  # A (duplicate return point)
                pruned.pop()  # B (spike tip)

        # Remove short local loops that look like meaningless cul-de-sac branches on map.
        # Example pattern: A -> ... -> B where B is very close to A (within a few meters).
        changed = True
        while changed and len(pruned) >= 4:
            changed = False
            i = 0
            while i < len(pruned) - 3:
                removed = False
                max_j = min(len(pruned) - 1, i + 10)
                for j in range(max_j, i + 1, -1):
                    closure_distance = _coord_distance_meters(pruned[i], pruned[j])
                    if closure_distance > 6.0:
                        continue
                    loop_distance = 0.0
                    for k in range(i, j):
                        loop_distance += _coord_distance_meters(pruned[k], pruned[k + 1])
                    if loop_distance <= 120.0 and loop_distance > closure_distance + 1.0:
                        del pruned[i + 1 : j]
                        changed = True
                        removed = True
                        break
                if removed:
                    continue
                i += 1

        if not pruned:
            return pruned

        # Enforce "single-stroke" geometry by collapsing any revisit loop:
        # if current point comes back near an already-visited point, drop the
        # intermediate cycle segment.
        revisit_threshold_m = 8.0
        revisit_collapsed = list(pruned)
        changed = True
        while changed and len(revisit_collapsed) >= 3:
            changed = False
            i = 2
            while i < len(revisit_collapsed):
                revisit_index: Optional[int] = None
                for j in range(0, i - 1):
                    if (
                        _coord_distance_meters(revisit_collapsed[j], revisit_collapsed[i])
                        <= revisit_threshold_m
                    ):
                        revisit_index = j
                        break

                if revisit_index is not None and i - revisit_index > 1:
                    del revisit_collapsed[revisit_index + 1 : i]
                    changed = True
                    i = max(2, revisit_index + 1)
                else:
                    i += 1

        compacted: List[List[float]] = [pruned[0]]
        for point in revisit_collapsed[1:]:
            if _coord_distance_meters(compacted[-1], point) > 0.3:
                compacted.append(point)
        return compacted

    features: List[Dict[str, Any]] = []
    total_distance = 0.0
    road_coords: List[List[float]] = []
    start_hint = [float(request.start.lng), float(request.start.lat)]

    for route_row in selected_routes:
        if not route_row.geojson:
            continue
        geom = json.loads(route_row.geojson)
        coords = _normalize_line_coordinates(geom.get("coordinates"))
        if len(coords) < 2:
            continue

        _append_continuous_segment(road_coords, coords, start_hint)

        if route_row.length:
            total_distance += float(route_row.length)

    road_coords = _prune_backtrack_spikes(road_coords)

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
            },
            "routing": {
                "strategy": route_strategy,
                "detour_ratio": round(detour_ratio, 3) if detour_ratio else None,
                "max_detour_ratio": max_detour_ratio,
                "weighted_distance_meters": weighted_distance,
                "shortest_distance_meters": shortest_distance,
                "safety_factor_min": sf_min,
                "safety_factor_max": sf_max,
                "safety_factor_exponent": sf_exp,
            },
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


@app.post("/v1/transit/plan", response_model=TransitPlanResponse)
async def plan_transit(body: TransitPlanRequest, db: AsyncSession = Depends(get_postgis_db)):
    departure_time = body.departure_time or datetime.utcnow()
    transit_provider = os.getenv("TRANSIT_PROVIDER", "csa").strip().lower()
    itineraries: List[TransitItineraryResponse] = []

    if transit_provider == "google":
        cache_key, departure_bucket = _build_transit_cache_key(
            body, transit_provider, departure_time
        )
        cached_itinerary_payload = await _get_cached_transit_itinerary(db, cache_key)

        if cached_itinerary_payload:
            try:
                logger.info(
                    "Transit cache hit for provider=%s key=%s", transit_provider, cache_key[:12]
                )
                itinerary_model = await _build_weighted_transit_itinerary(
                    itinerary_payload=cached_itinerary_payload,
                    body=body,
                    db=db,
                )
                itineraries.append(itinerary_model)
            except Exception as e:
                logger.warning("Transit cache payload invalid, fallback to provider call: %s", e)

        if not itineraries:
            logger.info(
                "Transit cache miss for provider=%s key=%s", transit_provider, cache_key[:12]
            )
            itinerary = await _plan_transit_with_google(body, departure_time)
            if itinerary:
                itinerary_payload = dict(itinerary)
                itinerary_model_for_cache = TransitItineraryResponse(
                    **{
                        **itinerary_payload,
                        "legs": [
                            TransitLegResponse(**leg) for leg in itinerary_payload.get("legs", [])
                        ],
                    }
                )
                await _store_transit_itinerary_cache(
                    db=db,
                    cache_key=cache_key,
                    provider=transit_provider,
                    body=body,
                    departure_bucket=departure_bucket,
                    itinerary_payload=itinerary_model_for_cache.model_dump(mode="json"),
                )
                itinerary_model = await _build_weighted_transit_itinerary(
                    itinerary_payload=itinerary_payload,
                    body=body,
                    db=db,
                )
                itineraries.append(itinerary_model)

        return TransitPlanResponse(
            algorithm="google_transit",
            generated_at=datetime.utcnow(),
            schedule_version="google-routes-v2",
            schedule_path="google://routes.googleapis.com/directions/v2:computeRoutes",
            itineraries=itineraries,
        )

    configured_path = os.getenv("TRANSIT_CSA_SCHEDULE_PATH")
    schedule_path = configured_path or str(DEFAULT_CSA_SCHEDULE_PATH)

    try:
        schedule = load_csa_schedule(schedule_path)
    except FileNotFoundError as e:
        logger.exception("CSA schedule file is missing.")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to load CSA schedule.")
        raise HTTPException(status_code=500, detail=f"Failed to load CSA schedule: {e}") from e

    itinerary = plan_journey_with_csa(
        schedule=schedule,
        origin_lat=body.origin.lat,
        origin_lon=body.origin.lon,
        destination_lat=body.destination.lat,
        destination_lon=body.destination.lon,
        departure_time=departure_time,
        max_walking_distance_m=body.max_walking_distance_m,
        max_transfers=body.max_transfers,
        search_window_minutes=body.search_window_minutes,
    )

    if itinerary:
        itinerary_payload = dict(itinerary)
        stop_coords_by_id = {
            stop_id: [stop.lon, stop.lat] for stop_id, stop in schedule.stops.items()
        }
        itinerary_model = await _build_weighted_transit_itinerary(
            itinerary_payload=itinerary_payload,
            body=body,
            db=db,
            stop_coords_by_id=stop_coords_by_id,
        )
        itineraries.append(itinerary_model)

    return TransitPlanResponse(
        algorithm="csa",
        generated_at=datetime.utcnow(),
        schedule_version=schedule.version,
        schedule_path=schedule_path,
        itineraries=itineraries,
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
    await cas_log.begin(Op.ROUTE_CALCULATE, {"user_id": body.user_id})

    ROUTING_ROUTE_CALCULATIONS_TOTAL.inc()

    rid = uuid.uuid4()
    now = datetime.utcnow()
    route_geojson: Optional[Dict[str, Any]] = None

    route_request = RouteRequest(
        start=Coordinate(lat=body.origin.lat, lng=body.origin.lon),
        end=Coordinate(lat=body.destination.lat, lng=body.destination.lon),
    )

    algorithm = _routing_algorithm_from_preferences(body.preferences.optimize_for)

    use_ch_engine = (
        ROUTE_ENGINE == "ch"
        and body.preferences.transport_mode == "walking"
        and CH_SUPPORTS_DYNAMIC_WEIGHTS
    )
    if ROUTE_ENGINE == "ch" and body.preferences.transport_mode == "walking" and not use_ch_engine:
        logger.info(
            "Skipping CH for walking route because CH_SUPPORTS_DYNAMIC_WEIGHTS is false; "
            "using weighted pgRouting to honor ways.safety_factor."
        )
    if use_ch_engine:
        try:
            route_geojson = await _compute_ch_route_geojson(
                request=route_request,
                optimize_for=body.preferences.optimize_for,
            )
            await cas_log.transition(
                Op.ROUTE_CALCULATE, "INIT", "ROUTE_COMPUTED", {"route_id": str(rid)}
            )
        except Exception as e:
            logger.warning("CH route failed in /v1/routes/calculate: %s", e)
            if not CH_FALLBACK_TO_DIJKSTRA:
                if isinstance(e, HTTPException):
                    raise
                raise HTTPException(status_code=502, detail=f"CH route failed: {e}") from e

    if route_geojson is None:
        try:
            route_geojson = await _compute_weighted_route_geojson(
                request=route_request,
                db=postgisDB,
                algorithm=algorithm,
            )
            await cas_log.transition(
                Op.ROUTE_CALCULATE, "INIT", "ROUTE_COMPUTED", {"route_id": str(rid)}
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
        await cas_log.transition(
            Op.ROUTE_CALCULATE, "INIT", "ROUTE_FALLBACK", {"route_id": str(rid)}
        )
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
    await cas_log.transition(
        Op.ROUTE_CALCULATE,
        "ROUTE_COMPUTED" if route_geojson else "ROUTE_FALLBACK",
        "COMMITTED",
    )

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
    await cas_log.begin(
        Op.NAVIGATION_START,
        {"route_id": str(body.route_id), "user_id": body.user_id},
    )
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
    await cas_log.transition(
        Op.NAVIGATION_START, "INIT", "SESSION_CREATED", {"session_id": str(sid)}
    )
    await cas_log.transition(Op.NAVIGATION_START, "SESSION_CREATED", "COMMITTED")

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
