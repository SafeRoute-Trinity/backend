from fastapi.testclient import TestClient

from services.routing_service.main import app

client = TestClient(app)


def test_transit_plan_returns_csa_itinerary():
    payload = {
        "origin": {"lat": 53.3438, "lon": -6.2546},
        "destination": {"lat": 53.3463, "lon": -6.2922},
        "departure_time": "2026-03-12T07:58:00+00:00",
        "max_walking_distance_m": 1200,
        "max_transfers": 3,
        "search_window_minutes": 180,
    }

    response = client.post("/v1/transit/plan", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["algorithm"] == "csa"
    assert "itineraries" in data
    assert len(data["itineraries"]) >= 1

    itinerary = data["itineraries"][0]
    assert itinerary["duration_s"] > 0
    assert itinerary["transfers"] >= 0
    assert len(itinerary["legs"]) >= 1
    assert any(leg["mode"] == "transit" for leg in itinerary["legs"])


def test_transit_plan_returns_empty_when_outside_coverage():
    payload = {
        "origin": {"lat": 0.0, "lon": 0.0},
        "destination": {"lat": 53.3463, "lon": -6.2922},
        "departure_time": "2026-03-12T07:58:00+00:00",
        "max_walking_distance_m": 300,
        "max_transfers": 3,
        "search_window_minutes": 180,
    }

    response = client.post("/v1/transit/plan", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert data["algorithm"] == "csa"
    assert data["itineraries"] == []
