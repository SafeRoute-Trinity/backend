##pytest services/notification/tests/test_main.py -q

import uuid

from fastapi.testclient import TestClient

from services.notification.main import app

client = TestClient(app)


# ========== Test Cases ==========


def test_root_endpoint():
    """Test root endpoint returns service info"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "notification"
    assert data["status"] == "running"


def test_create_and_get_notification_status():
    """Test creating SOS notification and retrieving status"""
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


def test_emergency_sms_endpoint():
    """Test emergency SMS endpoint"""
    sos_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    payload = {
        "sos_id": sos_id,
        "user_id": user_id,
        "location": {"lat": 53.34, "lon": -6.26, "accuracy_m": 10.0},
        "emergency_contact": {"name": "Bob", "phone": "+353800000222"},
        "message_template": "sos_alert",
        "variables": {"name": "Bob", "location": "Trinity College"},
        "notification_type": "sos",
        "locale": "en",
    }

    response = client.post("/v1/notifications/sos/sms", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert "status" in data
    assert data["status"] in ["sent", "failed"]
    assert "sms_id" in data
    assert "timestamp" in data
    assert "message_sent" in data
    assert "recipient" in data


def test_emergency_call_endpoint():
    """Test emergency call endpoint"""
    sos_id = str(uuid.uuid4())

    payload = {
        "sos_id": sos_id,
        "phone_number": "+112",
        "user_location": {"lat": 53.34, "lon": -6.26},
        "call_reason": "Safety emergency",
    }

    response = client.post("/v1/notifications/sos/call", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert "status" in data
    assert data["status"] in ["initiated", "failed"]
    assert "call_id" in data
    assert "timestamp" in data


def test_emergency_sms_missing_fields():
    """Test emergency SMS with missing required fields"""
    payload = {
        "sos_id": str(uuid.uuid4()),
        # Missing user_id, emergency_contact, etc.
    }

    response = client.post("/v1/notifications/sos/sms", json=payload)
    # Should fail validation
    assert response.status_code == 422


def test_emergency_call_missing_fields():
    """Test emergency call with missing required fields"""
    payload = {
        "sos_id": str(uuid.uuid4()),
        # Missing phone_number, user_location, call_reason
    }

    response = client.post("/v1/notifications/sos/call", json=payload)
    # Should fail validation
    assert response.status_code == 422


def test_get_nonexistent_notification_status():
    """Test retrieving status of non-existent notification"""
    fake_id = str(uuid.uuid4())
    response = client.get(f"/v1/notifications/{fake_id}")

    # The service should handle this gracefully
    # Either 404 or return a default status
    assert response.status_code in [200, 404]


def test_test_sms_endpoint():
    """Test the test SMS endpoint (note: will fail if Twilio not configured)"""
    payload = {"to_phone": "+1234567890", "message": "Test SMS from unit tests"}

    response = client.post("/v1/test/sms", json=payload)

    # Could be 200 (success) or 500 (Twilio not configured in test env)
    assert response.status_code in [200, 500]

    if response.status_code == 200:
        data = response.json()
        assert "status" in data
        assert "to" in data
        assert data["message"] == "Test SMS from unit tests"


def test_test_sms_missing_fields():
    """Test test SMS endpoint with missing fields"""
    payload = {
        "to_phone": "+1234567890",
        # Missing message
    }

    response = client.post("/v1/test/sms", json=payload)
    assert response.status_code == 422


# def test_create_sos_with_different_locales():
#     """Test creating SOS notifications with different locales"""
#     for locale in ["en", "es", "fr", "de"]:
#         req = {
#             "sos_id": f"SOS-{locale}",
#             "user_id": "usr_demo",
#             "location": {"lat": 53.34, "lon": -6.26},
#             "emergency_contact": {"name": "Contact", "phone": "+353800000999"},
#             "call_number": "+112",
#             "notification_type": "sos",
#             "locale": locale,
#             "variables": {"name": "User", "loc": "Location"},
#         }
#         r = client.post("/v1/notifications/sos", json=req)
#         assert r.status_code == 200


def test_notification_status_check_multiple_times():
    """Test checking notification status multiple times"""
    req = {
        "sos_id": "SOS-MULTI",
        "user_id": "usr_demo",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Alice", "phone": "+353800000111"},
        "call_number": "+112",
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Alice"},
    }
    r = client.post("/v1/notifications/sos", json=req)
    assert r.status_code == 200
    nid = r.json()["notification_id"]

    # Check status multiple times
    for _ in range(3):
        r2 = client.get(f"/v1/notifications/{nid}")
        assert r2.status_code == 200
        assert r2.json()["notification_id"] == nid
