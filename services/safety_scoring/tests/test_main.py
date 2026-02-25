import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.safety_scoring.main as sm
from services.safety_scoring import main
from services.safety_scoring.main import app


# ----------------------------
# Helpers: fake db + fake result
# ----------------------------
class FakeResult:
    def __init__(self, *, fetchone=None, fetchall=None):
        self._fetchone = fetchone
        self._fetchall = fetchall

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class FakeDB:
    """
    Minimal AsyncSession-like fake:
    - execute() returns FakeResult objects from plan
    - commit()/rollback() toggle flags
    """

    def __init__(self, plan=None, *, execute_raises: Exception | None = None):
        self.plan = list(plan or [])
        self.execute_raises = execute_raises
        self.committed = False
        self.rolled_back = False
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt, params=None):
        if self.execute_raises:
            raise self.execute_raises
        if not self.plan:
            return FakeResult(fetchone=None, fetchall=[])
        return self.plan.pop(0)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _override_safety_db(fake_db: FakeDB):
    """
    Override the ONLY db dependency used by this service: get_safety_scoring_db
    """

    async def override_dep():
        yield fake_db

    app.dependency_overrides[sm.get_safety_scoring_db] = override_dep


@pytest.fixture()
def api_client():
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------
# Basic endpoints
# ----------------------------
def test_root(api_client):
    r = api_client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "safety_scoring"
    assert r.json()["status"] == "running"


def test_health(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "safety_scoring"}


def test_metrics(api_client):
    r = api_client.get("/metrics")
    assert r.status_code == 200
    assert "service_requests_total" in r.text
    assert "service_request_duration_seconds" in r.text


# ----------------------------
# v1 endpoints (pure stubs)
# ----------------------------
def test_v1_score_route_stub(api_client, monkeypatch):
    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(sm, "SAFETY_SCORE_ROUTE_REQUESTS_TOTAL", counter, raising=False)

    payload = {
        "route_geometry": "LINESTRING(0 0, 1 1)",
        "segments": [
            {"start_lat": 53.35, "start_lon": -6.26, "end_lat": 53.36, "end_lon": -6.25},
            {"start_lat": 53.36, "start_lon": -6.25, "end_lat": 53.37, "end_lon": -6.24},
        ],
        "time_of_day": datetime.now(timezone.utc).isoformat(),
        "weather_conditions": "clear",
    }
    r = api_client.post("/v1/safety/score-route", json=payload)
    assert r.status_code == 200, r.text

    data = r.json()
    assert data["overall_score"] == 87.5
    assert len(data["segments"]) == 2
    assert data["segments"][0]["segment_id"] == "seg_001"
    assert counter.count == 1


def test_v1_update_weights_stub(api_client, monkeypatch):
    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(sm, "SAFETY_WEIGHTS_UPDATES_TOTAL", counter, raising=False)

    payload = {
        "user_id": "user-123",
        "weights": {
            "cctv_coverage": 1.0,
            "street_lighting": 2.0,
            "business_activity": 3.0,
            "crime_rate": 4.0,
            "pedestrian_traffic": 5.0,
        },
    }
    r = api_client.put("/v1/safety/weights", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "updated"
    assert data["weights_sum"] == 15.0
    assert counter.count == 1


def test_v1_safety_factors_stub_get_with_body(api_client, monkeypatch):
    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(sm, "SAFETY_FACTORS_QUERIES_TOTAL", counter, raising=False)

    payload = {"lat": 53.3498, "lon": -6.2603, "radius_m": 80}
    r = api_client.request("GET", "/v1/safety/factors", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["radius_m"] == 80
    assert "queried_at" in data
    assert counter.count == 1


# ----------------------------
# /api/danger_zones
# ----------------------------
def test_get_danger_zones_success(api_client):
    fake_rows = [
        SimpleNamespace(
            gid=1,
            safety_factor=1.5,
            geojson=json.dumps({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}),
        ),
        SimpleNamespace(
            gid=2,
            safety_factor=0.8,
            geojson=json.dumps({"type": "LineString", "coordinates": [[1, 1], [2, 2]]}),
        ),
    ]
    fake_db = FakeDB(plan=[FakeResult(fetchall=fake_rows)])
    _override_safety_db(fake_db)

    r = api_client.get("/api/danger_zones")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 2
    assert data["features"][0]["properties"]["weight"] == 1.5


def test_update_danger_zone_not_found_404(api_client):
    fake_db = FakeDB(plan=[FakeResult(fetchone=None)])  # SELECT geometry returns None
    _override_safety_db(fake_db)

    r = api_client.post("/api/danger_zones", json={"edge_id": 999, "safety_factor": 2.0})
    assert r.status_code == 404
    assert r.json()["detail"] == "Edge not found"


def test_update_danger_zone_success(api_client, monkeypatch):
    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(sm, "SAFETY_WEIGHTS_UPDATES_TOTAL", counter, raising=False)

    fake_geom = object()
    fake_db = FakeDB(plan=[FakeResult(fetchone=(fake_geom,)), FakeResult(fetchall=[])])
    _override_safety_db(fake_db)

    r = api_client.post("/api/danger_zones", json={"edge_id": 123, "safety_factor": 1.7})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "updated"
    assert fake_db.committed is True
    assert counter.count == 1


def test_reset_danger_zone_success(api_client):
    fake_db = FakeDB(plan=[FakeResult(fetchall=[])])
    _override_safety_db(fake_db)

    r = api_client.delete("/api/danger_zones/321")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reset"
    assert fake_db.committed is True


# ----------------------------
# /api/graph
# ----------------------------
def test_get_graph_geojson_success(api_client):
    fake_rows = [
        SimpleNamespace(
            gid=10,
            source=1,
            target=2,
            geojson=json.dumps(
                {"type": "LineString", "coordinates": [[-6.26, 53.35], [-6.25, 53.36]]}
            ),
            safety_factor=1.0,
        )
    ]
    fake_db = FakeDB(plan=[FakeResult(fetchall=fake_rows)])
    _override_safety_db(fake_db)

    r = api_client.get(
        "/api/graph",
        params={"min_lng": -6.3, "min_lat": 53.3, "max_lng": -6.2, "max_lat": 53.4},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["type"] == "FeatureCollection"
    assert data["features"][0]["properties"]["id"] == 10


# ----------------------------
# /api/route (mock pgRouting)
# ----------------------------
def test_get_route_success(api_client, monkeypatch):
    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(sm, "SAFETY_SCORE_ROUTE_REQUESTS_TOTAL", counter, raising=False)

    routes = [
        SimpleNamespace(
            seq=0,
            path_seq=0,
            node=1001,
            edge=11,
            cost=1.0,
            agg_cost=1.0,
            geojson=json.dumps(
                {"type": "LineString", "coordinates": [[-6.26, 53.35], [-6.255, 53.355]]}
            ),
            length=100.0,
            source=1001,
            target=1002,
        ),
        SimpleNamespace(
            seq=1,
            path_seq=1,
            node=1002,
            edge=12,
            cost=1.0,
            agg_cost=2.0,
            geojson=json.dumps(
                {"type": "LineString", "coordinates": [[-6.255, 53.355], [-6.25, 53.36]]}
            ),
            length=150.0,
            source=1002,
            target=2002,
        ),
    ]

    fake_db = FakeDB(
        plan=[
            FakeResult(fetchone=(1001,)),  # start nearest node
            FakeResult(fetchone=(2002,)),  # end nearest node
            FakeResult(fetchall=routes),  # first expansion returns a route
        ]
    )
    _override_safety_db(fake_db)

    payload = {"start": {"lat": 53.3498, "lng": -6.2603}, "end": {"lat": 53.3601, "lng": -6.2502}}
    r = api_client.post("/api/route?algorithm=dijkstra", json=payload)
    assert r.status_code == 200, r.text

    data = r.json()
    assert data["type"] == "FeatureCollection"
    assert data["properties"]["summary"]["distance_meters"] == 250.0
    assert counter.count == 1


def test_api_route_ch_success(api_client, monkeypatch):
    async def _fake_ch(_req):
        return {
            "type": "FeatureCollection",
            "features": [],
            "properties": {
                "summary": {"distance_meters": 10.0, "distance_km": 0.01, "duration": 5.0}
            },
        }

    monkeypatch.setattr(main, "get_ch_route_geojson", _fake_ch)

    # even for CH, dependency is resolved first; override it anyway
    fake_db = FakeDB(plan=[])
    _override_safety_db(fake_db)

    req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
    r = api_client.post("/api/route?algorithm=ch", json=req)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "FeatureCollection"


def test_api_route_invalid_algorithm(api_client):
    fake_db = FakeDB(plan=[])
    _override_safety_db(fake_db)

    req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
    r = api_client.post("/api/route?algorithm=bad_algo", json=req)
    assert r.status_code == 422
