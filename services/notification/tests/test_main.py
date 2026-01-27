##pytest services/notification/tests/test_main.py -q

from fastapi.testclient import TestClient

from services.notification.main import app

client = TestClient(app)


def test_create_and_get_notification_status():
    req = {
        "sos_id": "SOS-001",
        "user_id": "usr_demo",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Alice", "phone": "+353800000111"},
        "call_number": "+112",
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Alice", "loc": "Front Gate"},
    }
    r = client.post("/v1/notifications/sos", json=req)
    assert r.status_code == 200
    nid = r.json()["notification_id"]

    r2 = client.get(f"/v1/notifications/{nid}")
    assert r2.status_code == 200
    d = r2.json()
    assert d["notification_id"] == nid
    assert d["sos_id"] == "SOS-001"
    assert d["status"] in ["queued", "sending", "delivered", "failed", "partial"]
