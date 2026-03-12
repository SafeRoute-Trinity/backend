from types import SimpleNamespace

from fastapi.testclient import TestClient

from services.safety_scoring import main
from services.safety_scoring.main import (
    app,
    get_db,
    get_postgis_db,
    get_safety_scoring_db,
)

client = TestClient(app)


class _FakeResult:
    def __init__(self, one=None, all_rows=None):
        self._one = one
        self._all = all_rows or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _FakeRouteSession:
    def __init__(self):
        self._node_calls = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM nearest_edge" in sql:
            self._node_calls += 1
            return _FakeResult(one=(101 if self._node_calls == 1 else 202,))
        if "FROM pgr_" in sql:
            route_rows = [
                SimpleNamespace(
                    seq=1,
                    path_seq=1,
                    node=101,
                    edge=1,
                    cost=1.0,
                    agg_cost=1.0,
                    geojson='{"type":"LineString","coordinates":[[-6.26,53.34],[-6.25,53.35]]}',
                    length=100.0,
                    source=101,
                    target=202,
                )
            ]
            return _FakeResult(all_rows=route_rows)
        return _FakeResult()


async def _override_route_db():
    yield _FakeRouteSession()


def _install_route_override():
    app.dependency_overrides[get_safety_scoring_db] = _override_route_db
    app.dependency_overrides[get_db] = _override_route_db
    app.dependency_overrides[get_postgis_db] = _override_route_db


def _clear_overrides():
    app.dependency_overrides.pop(get_safety_scoring_db, None)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_postgis_db, None)


def test_api_route_dijkstra_success():
    _install_route_override()
    try:
        req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
        r = client.post("/api/route?algorithm=dijkstra", json=req)
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "FeatureCollection"
        assert len(d["features"]) >= 1
        assert d["properties"]["summary"]["distance_meters"] > 0
    finally:
        _clear_overrides()


def test_api_route_ch_success(monkeypatch):
    async def _fake_ch(_req):
        return {
            "type": "FeatureCollection",
            "features": [],
            "properties": {
                "summary": {"distance_meters": 10.0, "distance_km": 0.01, "duration": 5.0}
            },
        }

    monkeypatch.setattr(main, "get_ch_route_geojson", _fake_ch)
    req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
    r = client.post("/api/route?algorithm=ch", json=req)
    assert r.status_code == 200
    assert r.json()["type"] == "FeatureCollection"


def test_api_route_invalid_algorithm():
    req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
    r = client.post("/api/route?algorithm=bad_algo", json=req)
    assert r.status_code == 422
