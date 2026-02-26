# Run:
# uvicorn services.graphhopper_proxy.main:app --host 0.0.0.0 --port 20007 --reload

import os
import sys
from pathlib import Path
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Load backend .env for local development convenience.
backend_env_path = Path(__file__).resolve().parents[2] / ".env"
if backend_env_path.exists():
    load_dotenv(backend_env_path)

app = FastAPI(title="GraphHopper Proxy Service", description="GraphHopper CH route proxy.")

GRAPHHOPPER_BASE_URL = os.getenv("GRAPHHOPPER_BASE_URL", "http://127.0.0.1:8989")
GRAPHHOPPER_ROUTE_PATH = os.getenv("GRAPHHOPPER_ROUTE_PATH", "/route")
GRAPHHOPPER_PROFILE = os.getenv("GRAPHHOPPER_PROFILE", "foot")
GRAPHHOPPER_TIMEOUT_SECONDS = float(os.getenv("GRAPHHOPPER_TIMEOUT_SECONDS", "8"))
GRAPHHOPPER_API_KEY = os.getenv("GRAPHHOPPER_API_KEY")


class Coordinate(BaseModel):
    lat: float
    lng: float


class RouteRequest(BaseModel):
    start: Coordinate
    end: Coordinate


@app.get("/")
async def root():
    return {"service": "graphhopper_proxy", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "graphhopper_proxy"}


@app.get("/health/deps")
async def health_deps():
    route_url = f"{GRAPHHOPPER_BASE_URL.rstrip('/')}{GRAPHHOPPER_ROUTE_PATH}"
    probe_params = {
        "profile": GRAPHHOPPER_PROFILE,
        "point": ["53.35,-6.26", "53.34,-6.27"],
        "points_encoded": "false",
        "ch.disable": "false",
    }
    if GRAPHHOPPER_API_KEY:
        probe_params["key"] = GRAPHHOPPER_API_KEY

    try:
        async with httpx.AsyncClient(timeout=min(GRAPHHOPPER_TIMEOUT_SECONDS, 3.0)) as client:
            resp = await client.get(route_url, params=probe_params)
        return {
            "service": "graphhopper_proxy",
            "graphhopper_up": resp.status_code < 500,
            "graphhopper_status": resp.status_code,
        }
    except Exception as e:
        return {
            "service": "graphhopper_proxy",
            "graphhopper_up": False,
            "error": str(e),
        }


@app.post("/api/route")
async def get_route(
    request: RouteRequest,
    algorithm: Literal["ch"] = Query("ch"),
    profile: Optional[str] = Query(None),
):
    if algorithm != "ch":
        raise HTTPException(status_code=400, detail="Only algorithm=ch is supported.")

    route_url = f"{GRAPHHOPPER_BASE_URL.rstrip('/')}{GRAPHHOPPER_ROUTE_PATH}"
    chosen_profile = profile or GRAPHHOPPER_PROFILE
    params = {
        "profile": chosen_profile,
        "point": [
            f"{request.start.lat},{request.start.lng}",
            f"{request.end.lat},{request.end.lng}",
        ],
        "points_encoded": "false",
        "ch.disable": "false",
    }
    if GRAPHHOPPER_API_KEY:
        params["key"] = GRAPHHOPPER_API_KEY

    try:
        async with httpx.AsyncClient(timeout=GRAPHHOPPER_TIMEOUT_SECONDS) as client:
            resp = await client.get(route_url, params=params)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GraphHopper request failed: {e}") from e

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GraphHopper error: {resp.text[:300]}")

    data = resp.json()
    paths = data.get("paths") or []
    if not paths:
        raise HTTPException(status_code=404, detail="No CH path found.")

    path = paths[0]
    points = (path.get("points") or {}).get("coordinates") or []
    if not points:
        raise HTTPException(status_code=404, detail="No route geometry in CH response.")

    distance_m = float(path.get("distance", 0.0))
    duration_s = float(path.get("time", 0.0)) / 1000.0

    features = [
        {
            "type": "Feature",
            "properties": {"type": "connector"},
            "geometry": {
                "type": "LineString",
                "coordinates": [[request.start.lng, request.start.lat], points[0]],
            },
        },
        {
            "type": "Feature",
            "properties": {"type": "road"},
            "geometry": {"type": "LineString", "coordinates": points},
        },
        {
            "type": "Feature",
            "properties": {"type": "connector"},
            "geometry": {
                "type": "LineString",
                "coordinates": [points[-1], [request.end.lng, request.end.lat]],
            },
        },
    ]

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "summary": {
                "distance_meters": distance_m,
                "distance_km": round(distance_m / 1000, 2),
                "duration": duration_s,
            },
            "engine": "graphhopper_ch",
            "profile": chosen_profile,
        },
    }
