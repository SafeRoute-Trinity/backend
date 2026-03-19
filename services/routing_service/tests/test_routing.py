# pytest services/routing_service/tests/test_routing.py -q

import uuid
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from services.routing_service.main import NAV, ROUTES, app, get_db, get_postgis_db

client = TestClient(app)


# ---------- Fake DB sessions for dependency override ----------


class _FakeResult:
    def __init__(self, one=None, all_rows=None):
        self._one = one
        self._all = all_rows if all_rows is not None else []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _FakeDbSession:
    """Minimal session for get_db: audit add/commit only."""

    def add(self, x):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _FakePostgisSessionSuccess:
    """PostGIS session that returns valid nodes and route rows (for success path)."""

    def __init__(self):
        self._node_calls = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "nearest_edge" in sql or "FROM nearest_edge" in sql:
            self._node_calls += 1
            return _FakeResult(one=(101 if self._node_calls == 1 else 202,))
        if "pgr_" in sql:
            rows = [
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
            return _FakeResult(all_rows=rows)
        return _FakeResult()


class _FakePostgisSessionNoNodes:
    """PostGIS session: node query returns None -> 404 'Could not find nearest road nodes'."""

    async def execute(self, stmt, params=None):
        return _FakeResult(one=None)


class _FakePostgisSessionNoPath:
    """PostGIS session: nodes ok, routing fetchall empty -> 404 'No path found'."""

    def __init__(self):
        self._node_calls = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "nearest_edge" in sql or "FROM nearest_edge" in sql:
            self._node_calls += 1
            return _FakeResult(one=(101 if self._node_calls == 1 else 202,))
        if "pgr_" in sql:
            return _FakeResult(all_rows=[])
        return _FakeResult()


class _FakePostgisSessionRaises:
    """PostGIS session: execute raises -> calc() catches and uses fallback response."""

    async def execute(self, stmt, params=None):
        raise RuntimeError("simulated downstream error")


class _FakePostgisSessionFirstFetchNone:
    """PostGIS session: first fetchone() returns None (e.g. edge not found for update/reset)."""

    async def execute(self, stmt, params=None):
        return _FakeResult(one=None)

    async def commit(self):
        pass

    async def rollback(self):
        pass


async def _override_get_db():
    yield _FakeDbSession()


async def _override_postgis_success():
    yield _FakePostgisSessionSuccess()


async def _override_postgis_no_nodes():
    yield _FakePostgisSessionNoNodes()


async def _override_postgis_no_path():
    yield _FakePostgisSessionNoPath()


async def _override_postgis_raises():
    yield _FakePostgisSessionRaises()


async def _override_postgis_first_fetch_none():
    yield _FakePostgisSessionFirstFetchNone()


def _install_overrides(get_override, postgis_override):
    app.dependency_overrides[get_db] = get_override
    app.dependency_overrides[get_postgis_db] = postgis_override


def _clear_overrides():
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_postgis_db, None)


# ========== Test Cases ==========


def test_root_endpoint():
    """Test root endpoint returns service info"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "routing_service"
    assert data["status"] == "running"


def test_health_check():
    """Test health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "routing_service"


def test_calculate_route():
    """Test route calculation with valid request"""
    user_id = "test-user-routing"
    payload = {
        "origin": {"lat": 53.3498, "lon": -6.2603},
        "destination": {"lat": 53.3398, "lon": -6.2503},
        "user_id": user_id,
        "preferences": {
            "optimize_for": "safety",
            "transport_mode": "walking",
        },
    }

    response = client.post("/v1/routes/calculate", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Verify response structure
    assert "route_id" in data
    assert "routes" in data
    assert "alternatives_count" in data
    assert "calculated_at" in data
    assert len(data["routes"]) > 0

    # Verify route structure
    route = data["routes"][0]
    assert route["route_index"] == 0
    assert route["is_primary"] is True
    assert "geometry" in route
    assert "distance_m" in route
    assert "duration_s" in route
    assert "safety_score" in route
    assert "waypoints" in route


def test_calculate_route_invalid_coordinates():
    """Test route calculation with invalid coordinates"""
    user_id = "test-user-routing"
    payload = {
        "origin": {"lat": 200.0, "lon": -6.2603},  # Invalid latitude
        "destination": {"lat": 53.3398, "lon": -6.2503},
        "user_id": user_id,
        "preferences": {
            "optimize_for": "safety",
            "transport_mode": "walking",
        },
    }

    response = client.post("/v1/routes/calculate", json=payload)
    # FastAPI validation should catch this
    assert response.status_code == 422


def test_calculate_route_missing_fields():
    """Test route calculation with missing required fields"""
    payload = {
        "origin": {"lat": 53.3498, "lon": -6.2603},
        # Missing destination
        "user_id": "test-user-routing",
    }

    response = client.post("/v1/routes/calculate", json=payload)
    assert response.status_code == 422


def test_calculate_route_different_optimization():
    """Test route calculation with different optimization preferences"""
    user_id = "test-user-routing"

    for optimize_for in ["safety", "time", "distance", "balanced"]:
        payload = {
            "origin": {"lat": 53.3498, "lon": -6.2603},
            "destination": {"lat": 53.3398, "lon": -6.2503},
            "user_id": user_id,
            "preferences": {
                "optimize_for": optimize_for,
                "transport_mode": "walking",
            },
        }

        response = client.post("/v1/routes/calculate", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "route_id" in data


def test_recalculate_route():
    """Test route recalculation"""
    route_id = str(uuid.uuid4())
    payload = {
        "route_id": route_id,
        "current_location": {"lat": 53.3450, "lon": -6.2550},
        "reason": "off_track",
    }

    response = client.post(f"/v1/routes/{route_id}/recalculate", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Should return a new route
    assert "route_id" in data
    assert "routes" in data
    assert len(data["routes"]) > 0


def test_recalculate_route_different_reasons():
    """Test route recalculation with different reasons"""
    route_id = str(uuid.uuid4())

    for reason in ["off_track", "road_closure", "user_request", "safety_alert"]:
        payload = {
            "route_id": route_id,
            "current_location": {"lat": 53.3450, "lon": -6.2550},
            "reason": reason,
        }

        response = client.post(f"/v1/routes/{route_id}/recalculate", json=payload)
        assert response.status_code == 200


def test_navigation_start():
    """Test navigation session start"""
    route_id = str(uuid.uuid4())
    user_id = "test-user-nav"

    payload = {
        "route_id": route_id,
        "user_id": user_id,
        "estimated_arrival": datetime.utcnow().isoformat(),
    }

    response = client.post("/v1/navigation/start", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert "session_id" in data
    assert data["status"] == "active"
    assert "started_at" in data


def test_navigation_start_missing_fields():
    """Test navigation start with missing fields"""
    payload = {
        "route_id": str(uuid.uuid4()),
        # Missing user_id and estimated_arrival
    }

    response = client.post("/v1/navigation/start", json=payload)
    assert response.status_code == 422


def test_metrics_endpoint():
    """Test Prometheus metrics endpoint"""
    response = client.get("/metrics")
    assert response.status_code == 200
    # Metrics should be in Prometheus text format
    assert "text/plain" in response.headers["content-type"]


# ---------- Tests with mocked DB (uncovered branches) ----------
#
# Coverage mapping (services/routing_service/main.py):
# - test_calculate_route_fallback_when_postgis_raises: calc() except block ~794-796, else branch ~809-821 (fallback route).
# - test_api_route_404_when_no_nearest_nodes: _compute_weighted_route_geojson ~381-382 (no start/end node).
# - test_api_route_404_when_no_path_found: _compute_weighted_route_geojson ~463-476 (empty routes -> 404).
# - test_api_route_success_with_mocked_postgis: get_route -> _compute_weighted_route_geojson success path.
# - test_recalculate_route_*: RecalculateRequest validation, path param validation (422).
# - test_navigation_start_invalid_*: NavigationStartRequest validation (422).
# - test_list_routes_*, test_list_navigation_sessions_*: list_routes ~806-866, list_navigation_sessions; Query ge=1.
# - test_get_graph_geojson_missing_params: get_graph_geojson Query params (422).
# - test_get_danger_zones_500_when_db_raises: get_danger_zones ~644-645 (exception -> 500).
# - test_update_danger_zone_404_*: update_danger_zone ~660-661 (geom_res None -> 404).
# - test_reset_danger_zone_404_*: reset_danger_zone ~698-699 (geom_res None -> 404).


def test_calculate_route_fallback_when_postgis_raises():
    """
    When _compute_weighted_route_geojson raises (e.g. downstream/DB error),
    calc() catches and returns fallback route (main.py ~794-821 else branch).
    """
    _install_overrides(_override_get_db, _override_postgis_raises)
    try:
        payload = {
            "origin": {"lat": 53.35, "lon": -6.26},
            "destination": {"lat": 53.34, "lon": -6.25},
            "user_id": "test-user",
            "preferences": {"optimize_for": "safety", "transport_mode": "walking"},
        }
        response = client.post("/v1/routes/calculate", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "route_id" in data
        assert len(data["routes"]) == 1
        assert data["routes"][0]["geometry"] == "encoded_polyline_demo"
        assert data["routes"][0]["distance_m"] == 2450
        assert data["routes"][0]["duration_s"] == 1800
    finally:
        _clear_overrides()


def test_api_route_404_when_no_nearest_nodes():
    """
    POST /api/route when DB returns no start/end node -> 404
    (main.py _compute_weighted_route_geojson ~381-382).
    """
    _install_overrides(_override_get_db, _override_postgis_no_nodes)
    try:
        req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
        response = client.post("/api/route?algorithm=dijkstra", json=req)
        assert response.status_code == 404
        assert "nearest road nodes" in response.json().get("detail", "")
    finally:
        _clear_overrides()


def test_api_route_404_when_no_path_found():
    """
    POST /api/route when routing query returns empty -> 404 'No path found'
    (main.py _compute_weighted_route_geojson ~463-476).
    """
    _install_overrides(_override_get_db, _override_postgis_no_path)
    try:
        req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
        response = client.post("/api/route?algorithm=dijkstra", json=req)
        assert response.status_code == 404
        assert "No path found" in response.json().get("detail", "")
    finally:
        _clear_overrides()


def test_api_route_success_with_mocked_postgis():
    """
    POST /api/route success path with mocked PostGIS (main.py get_route -> _compute_weighted_route_geojson).
    """
    _install_overrides(_override_get_db, _override_postgis_success)
    try:
        req = {"start": {"lat": 53.34, "lng": -6.26}, "end": {"lat": 53.35, "lng": -6.25}}
        response = client.post("/api/route?algorithm=dijkstra", json=req)
        assert response.status_code == 200
        d = response.json()
        assert d["type"] == "FeatureCollection"
        assert "features" in d
        assert "properties" in d and "summary" in d["properties"]
    finally:
        _clear_overrides()


def test_recalculate_route_missing_current_location():
    """Recalculate with missing current_location -> 422 (main.py RecalculateRequest)."""
    route_id = str(uuid.uuid4())
    payload = {"route_id": route_id, "reason": "off_track"}
    response = client.post(f"/v1/routes/{route_id}/recalculate", json=payload)
    assert response.status_code == 422


def test_recalculate_route_missing_reason():
    """Recalculate with missing reason -> 422 (main.py RecalculateRequest)."""
    route_id = str(uuid.uuid4())
    payload = {
        "route_id": route_id,
        "current_location": {"lat": 53.3450, "lon": -6.2550},
    }
    response = client.post(f"/v1/routes/{route_id}/recalculate", json=payload)
    assert response.status_code == 422


def test_recalculate_route_invalid_reason():
    """Recalculate with invalid reason enum -> 422 (main.py RecalculateRequest)."""
    route_id = str(uuid.uuid4())
    payload = {
        "route_id": route_id,
        "current_location": {"lat": 53.3450, "lon": -6.2550},
        "reason": "invalid_reason",
    }
    response = client.post(f"/v1/routes/{route_id}/recalculate", json=payload)
    assert response.status_code == 422


def test_recalculate_route_invalid_route_id_in_url():
    """Recalculate with non-UUID route_id in URL -> 422 (main.py path param)."""
    payload = {
        "route_id": str(uuid.uuid4()),
        "current_location": {"lat": 53.3450, "lon": -6.2550},
        "reason": "off_track",
    }
    response = client.post("/v1/routes/not-a-uuid/recalculate", json=payload)
    assert response.status_code == 422


def test_navigation_start_invalid_estimated_arrival():
    """Navigation start with invalid estimated_arrival format -> 422 (main.py NavigationStartRequest)."""
    payload = {
        "route_id": str(uuid.uuid4()),
        "user_id": "user-1",
        "estimated_arrival": "not-a-datetime",
    }
    response = client.post("/v1/navigation/start", json=payload)
    assert response.status_code == 422


def test_navigation_start_invalid_route_id_format():
    """Navigation start with invalid route_id in body -> 422 (main.py NavigationStartRequest)."""
    payload = {
        "route_id": "not-a-uuid",
        "user_id": "user-1",
        "estimated_arrival": datetime.utcnow().isoformat(),
    }
    response = client.post("/v1/navigation/start", json=payload)
    assert response.status_code == 422


def test_list_routes_success():
    """GET /v1/routes returns 200 and pagination structure (main.py list_routes ~806-866)."""
    # Clear in-memory store so response has consistent types (user_id in ROUTES is str; model expects Optional[UUID])
    saved_routes = dict(ROUTES)
    ROUTES.clear()
    try:
        response = client.get("/v1/routes?page=1&page_size=10")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "filters" in data
        assert "pagination" in data
        assert data["pagination"]["page"] == 1
        assert data["pagination"]["page_size"] == 10
    finally:
        ROUTES.clear()
        ROUTES.update(saved_routes)


def test_list_routes_invalid_page():
    """GET /v1/routes with page=0 -> 422 (main.py Query ge=1)."""
    response = client.get("/v1/routes?page=0&page_size=10")
    assert response.status_code == 422


def test_list_navigation_sessions_success():
    """GET /v1/navigation/sessions returns 200 and structure (main.py list_navigation_sessions)."""
    saved_nav = dict(NAV)
    NAV.clear()
    try:
        response = client.get("/v1/navigation/sessions?page=1&page_size=20")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert "pagination" in data
    finally:
        NAV.clear()
        NAV.update(saved_nav)


def test_list_navigation_sessions_invalid_page():
    """GET /v1/navigation/sessions with page=0 -> 422."""
    response = client.get("/v1/navigation/sessions?page=0&page_size=10")
    assert response.status_code == 422


def test_get_graph_geojson_missing_params():
    """GET /api/graph with missing query params -> 422 (main.py get_graph_geojson ~704-711)."""
    response = client.get("/api/graph")
    assert response.status_code == 422


def test_get_danger_zones_500_when_db_raises():
    """
    GET /api/danger_zones when DB execute raises -> 500
    (main.py get_danger_zones ~644-645).
    """
    _install_overrides(_override_get_db, _override_postgis_raises)
    try:
        response = client.get("/api/danger_zones")
        assert response.status_code == 500
        assert "detail" in response.json()
    finally:
        _clear_overrides()


def test_update_danger_zone_404_when_edge_not_found():
    """
    POST /api/danger_zones when edge id not in DB -> 404
    (main.py update_danger_zone ~660-661).
    """
    _install_overrides(_override_get_db, _override_postgis_first_fetch_none)
    try:
        payload = {"edge_id": 99999, "safety_factor": 1.5}
        response = client.post("/api/danger_zones", json=payload)
        assert response.status_code == 404
        assert "Edge not found" in response.json().get("detail", "")
    finally:
        _clear_overrides()


def test_reset_danger_zone_404_when_edge_not_found():
    """
    DELETE /api/danger_zones/:id when edge not in DB -> 404
    (main.py reset_danger_zone ~698-699).
    """
    _install_overrides(_override_get_db, _override_postgis_first_fetch_none)
    try:
        response = client.delete("/api/danger_zones/99999")
        assert response.status_code == 404
        assert "Edge not found" in response.json().get("detail", "")
    finally:
        _clear_overrides()
