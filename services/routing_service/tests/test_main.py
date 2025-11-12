###pytest services/routing_service/tests/test_main.py -q


from fastapi.testclient import TestClient
from services.routing_service.main import app

client = TestClient(app)

def test_calculate_route_and_start_navigation():
    calc_req = {
        "origin": {"lat": 53.342, "lon": -6.256},
        "destination": {"lat": 53.345, "lon": -6.262},
        "user_id": "usr_demo",
        "preferences": {"optimize_for": "balanced", "transport_mode": "walking"}
    }
    r = client.post("/v1/routes/calculate", json=calc_req)
    assert r.status_code == 200
    data = r.json()
    assert "route_id" in data
    assert data["alternatives_count"] >= 1

    start_req = {
        "route_id": data["route_id"],
        "user_id": "usr_demo",
        "estimated_arrival": "2025-11-07T10:30:00Z"
    }
    r2 = client.post("/v1/navigation/start", json=start_req)
    assert r2.status_code == 200
    assert r2.json()["status"] == "active"

def test_recalculate_route():
    calc_req = {
        "origin": {"lat": 53.34, "lon": -6.26},
        "destination": {"lat": 53.35, "lon": -6.27},
        "user_id": "usr_demo",
        "preferences": {"optimize_for": "balanced", "transport_mode": "walking"}
    }
    r = client.post("/v1/routes/calculate", json=calc_req)
    route_id = r.json()["route_id"]

    recalc_req = {
        "route_id": route_id,
        "current_location": {"lat": 53.341, "lon": -6.261},
        "reason": "off_track"
    }
    r2 = client.post(f"/v1/routes/{route_id}/recalculate", json=recalc_req)
    assert r2.status_code == 200
    assert "routes" in r2.json()
