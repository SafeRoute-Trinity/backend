###pytest services/safety_scoring/tests/test_main.py -q


from fastapi.testclient import TestClient

from services.safety_scoring.main import app

client = TestClient(app)


def test_score_route():
    req = {
        "route_geometry": "encoded_polyline_demo",
        "segments": [
            {
                "start_lat": 53.34,
                "start_lon": -6.26,
                "end_lat": 53.341,
                "end_lon": -6.261,
            },
            {
                "start_lat": 53.341,
                "start_lon": -6.261,
                "end_lat": 53.342,
                "end_lon": -6.262,
            },
        ],
        "time_of_day": "2025-11-07T10:00:00Z",
        "weather_conditions": "clear",
    }
    r = client.post("/v1/safety/score-route", json=req)
    assert r.status_code == 200
    d = r.json()
    assert "overall_score" in d
    assert len(d["segments"]) == 2


def test_safety_factors_and_weights():
    r = client.post(
        "/v1/safety/factors", json={"lat": 53.34, "lon": -6.26, "radius_m": 50}
    )
    assert r.status_code == 200
    assert "composite_score" in r.json()

    w_req = {
        "user_id": "usr_demo",
        "weights": {
            "cctv_coverage": 0.2,
            "street_lighting": 0.2,
            "business_activity": 0.2,
            "crime_rate": 0.2,
            "pedestrian_traffic": 0.2,
        },
    }
    r2 = client.put("/v1/safety/weights", json=w_req)
    assert r2.status_code == 200
    assert r2.json()["status"] == "updated"
