###pytest services/feedback/tests/test_main.py -q

import uuid
from datetime import datetime

from fastapi.testclient import TestClient

from services.feedback.main import app

client = TestClient(app)


# ========== Test Cases ==========


def test_root_endpoint():
    """Test root endpoint returns service info"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "feedback"
    assert data["status"] == "running"


def test_submit_and_status_feedback():
    """Test submitting feedback and retrieving its status"""
    req = {
        "feedback_id": "fbk_001",
        "user_id": "usr_demo",
        "type": "safety_issue",
        "description": "Broken street light",
        "severity": "high",
        "timestamp": "2025-11-07T10:00:00Z",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200
    ticket = r.json()["ticket_number"]
    assert ticket.startswith("TKT-")

    r2 = client.get("/v1/feedback/fbk_001/status")
    assert r2.status_code == 200
    assert r2.json()["feedback_id"] == "fbk_001"


def test_validate_feedback():
    """Test feedback validation endpoint"""
    r = client.post(
        "/v1/feedback/validate",
        json={"user_id": "usr_demo", "content": "nice", "recent_submissions_count": 0},
    )
    assert r.status_code == 200
    assert r.json()["allow_submission"] is True


def test_submit_feedback_different_types():
    """Test submitting different types of feedback"""
    for feedback_type in ["safety_issue", "route_quality", "other"]:
        req = {
            "feedback_id": f"fbk_{feedback_type}",
            "user_id": str(uuid.uuid4()),
            "type": feedback_type,
            "description": f"Test feedback for {feedback_type}",
            "severity": "medium",
            "timestamp": datetime.utcnow().isoformat(),
        }
        r = client.post("/v1/feedback/submit", json=req)
        assert r.status_code == 200
        assert "ticket_number" in r.json()


def test_submit_feedback_different_severities():
    """Test submitting feedback with different severity levels"""
    for severity in ["low", "medium", "high", "critical"]:
        req = {
            "feedback_id": f"fbk_{severity}",
            "user_id": str(uuid.uuid4()),
            "type": "safety_issue",
            "description": f"Test {severity} severity feedback",
            "severity": severity,
            "timestamp": datetime.utcnow().isoformat(),
        }
        r = client.post("/v1/feedback/submit", json=req)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "received"


def test_submit_feedback_with_location():
    """Test submitting feedback with location data"""
    req = {
        "feedback_id": "fbk_with_location",
        "user_id": str(uuid.uuid4()),
        "type": "safety_issue",
        "location": {"lat": 53.3498, "lon": -6.2603},
        "description": "Pothole at this location",
        "severity": "high",
        "timestamp": datetime.utcnow().isoformat(),
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200


def test_submit_feedback_with_route_and_session():
    """Test submitting feedback with route_id and session_id"""
    req = {
        "feedback_id": "fbk_with_route",
        "user_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "type": "route_quality",
        "description": "Route took me through unsafe area",
        "severity": "high",
        "timestamp": datetime.utcnow().isoformat(),
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200


def test_submit_feedback_missing_required_fields():
    """Test submitting feedback with missing required fields"""
    req = {
        "feedback_id": "fbk_incomplete",
        # Missing user_id, type, description, severity, timestamp
    }
    r = client.post("/v1/feedback/submit", json=req)
    # Should fail validation
    assert r.status_code == 422


def test_submit_feedback_invalid_type():
    """Test submitting feedback with invalid type"""
    req = {
        "feedback_id": "fbk_invalid",
        "user_id": str(uuid.uuid4()),
        "type": "invalid_type",  # Not in allowed values
        "description": "Test",
        "severity": "low",
        "timestamp": datetime.utcnow().isoformat(),
    }
    r = client.post("/v1/feedback/submit", json=req)
    # Should fail validation
    assert r.status_code == 422


def test_submit_feedback_invalid_severity():
    """Test submitting feedback with invalid severity"""
    req = {
        "feedback_id": "fbk_invalid_severity",
        "user_id": str(uuid.uuid4()),
        "type": "safety_issue",
        "description": "Test",
        "severity": "super_critical",  # Not in allowed values
        "timestamp": datetime.utcnow().isoformat(),
    }
    r = client.post("/v1/feedback/submit", json=req)
    # Should fail validation
    assert r.status_code == 422


def test_validate_feedback_spam_detection():
    """Test spam detection in feedback validation"""
    # Test with many recent submissions (potential spam)
    r = client.post(
        "/v1/feedback/validate",
        json={
            "user_id": str(uuid.uuid4()),
            "content": "spam spam spam",
            "recent_submissions_count": 10,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert "is_spam" in data
    assert "confidence" in data
    assert "allow_submission" in data


def test_validate_feedback_rate_limiting():
    """Test rate limiting validation"""
    # User with very high submission count
    r = client.post(
        "/v1/feedback/validate",
        json={
            "user_id": str(uuid.uuid4()),
            "content": "Legitimate feedback",
            "recent_submissions_count": 100,
        },
    )
    assert r.status_code == 200
    data = r.json()
    # Should likely be flagged or have warnings
    assert "flags" in data


def test_validate_feedback_missing_fields():
    """Test validation with missing required fields"""
    r = client.post(
        "/v1/feedback/validate",
        json={
            "user_id": str(uuid.uuid4()),
            # Missing content and recent_submissions_count
        },
    )
    # Should fail validation
    assert r.status_code == 422


def test_get_feedback_status_nonexistent():
    """Test retrieving status of non-existent feedback"""
    fake_id = str(uuid.uuid4())
    r = client.get(f"/v1/feedback/{fake_id}/status")

    # Should return 404 or handle gracefully
    assert r.status_code in [200, 404]


def test_get_feedback_status_multiple_times():
    """Test retrieving feedback status multiple times"""
    req = {
        "feedback_id": "fbk_multi_status",
        "user_id": str(uuid.uuid4()),
        "type": "other",
        "description": "Testing multiple status checks",
        "severity": "low",
        "timestamp": datetime.utcnow().isoformat(),
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200

    # Check status multiple times
    for _ in range(3):
        r2 = client.get("/v1/feedback/fbk_multi_status/status")
        assert r2.status_code == 200
        assert r2.json()["feedback_id"] == "fbk_multi_status"


def test_submit_feedback_with_attachments():
    """Test submitting feedback with attachment URLs"""
    req = {
        "feedback_id": "fbk_with_attachments",
        "user_id": str(uuid.uuid4()),
        "type": "safety_issue",
        "description": "Photo evidence of broken light",
        "severity": "high",
        "attachments": [
            "https://example.com/photo1.jpg",
            "https://example.com/photo2.jpg",
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200


def test_metrics_endpoint():
    """Test Prometheus metrics endpoint"""
    response = client.get("/metrics")
    assert response.status_code == 200
    # Metrics should be in Prometheus text format
    assert "text/plain" in response.headers["content-type"]
