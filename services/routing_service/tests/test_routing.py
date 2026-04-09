# pytest services/routing_service/tests/test_main.py -q

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from services.routing_service.main import app, get_postgis_db

client = TestClient(app)


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


def test_simple_risk_zones():
    """Test simplified high-risk zones endpoint."""

    async def override_get_postgis_db():
        mock_db = AsyncMock()
        mock_result = Mock()
        mock_mappings = Mock()
        mock_mappings.all.return_value = [
            {
                "gid": 123,
                "safety_factor": 1.7,
                "geojson": '{"type":"LineString","coordinates":[[1,2],[3,4]]}',
            }
        ]
        mock_result.mappings.return_value = mock_mappings
        mock_db.execute.return_value = mock_result
        yield mock_db

    app.dependency_overrides[get_postgis_db] = override_get_postgis_db
    try:
        response = client.get("/api/simple_risk_zones")
    finally:
        app.dependency_overrides.pop(get_postgis_db, None)

    assert response.status_code == 200
    data = response.json()
    assert "zones" in data
    assert data["zones"] == [
        {
            "id": 123,
            "safety_factor": 1.7,
            "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]},
        }
    ]


def test_high_risk_alert():
    """Test high-risk alert endpoint with nearby risky road segments."""

    async def override_get_postgis_db():
        mock_db = AsyncMock()
        mock_result = Mock()
        mock_mappings = Mock()
        mock_mappings.all.return_value = [
            {
                "gid": 321,
                "safety_factor": 1.8,
                "distance_m": 12.345,
                "geojson": '{"type":"LineString","coordinates":[[10,20],[30,40]]}',
            }
        ]
        mock_result.mappings.return_value = mock_mappings
        mock_db.execute.return_value = mock_result
        yield mock_db

    app.dependency_overrides[get_postgis_db] = override_get_postgis_db
    try:
        response = client.post(
            "/api/high_risk_alert",
            json={"lat": 53.3498, "lng": -6.2603, "radius_m": 30},
        )
    finally:
        app.dependency_overrides.pop(get_postgis_db, None)

    assert response.status_code == 200
    data = response.json()
    assert data["in_high_risk_area"] is True
    assert data["threshold"] == 1.5
    assert data["radius_m"] == 30
    assert data["user_location"] == {"lat": 53.3498, "lng": -6.2603}
    assert data["matches"] == [
        {
            "id": 321,
            "safety_factor": 1.8,
            "distance_m": 12.35,
            "geometry": {"type": "LineString", "coordinates": [[10, 20], [30, 40]]},
        }
    ]
