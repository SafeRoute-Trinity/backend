"""
Tests for feedback service endpoints.

Run with: pytest services/feedback/tests/test_main.py -v
"""

import pytest
from fastapi.testclient import TestClient

from services.feedback.main import app

# Mark all tests as unit tests
pytestmark = pytest.mark.unit

client = TestClient(app)


def test_submit_and_status_feedback():
    """Test submitting feedback and checking status."""
    req = {
        "feedback_id": "550e8400-e29b-41d4-a716-446655440000",
        "user_id": "550e8400-e29b-41d4-a716-446655440001",
        "type": "safety_issue",
        "description": "Broken street light",
        "severity": "high",
        "timestamp": "2025-11-07T10:00:00Z",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200
    ticket = r.json()["ticket_number"]
    assert ticket.startswith("TKT-")

    r2 = client.get("/v1/feedback/550e8400-e29b-41d4-a716-446655440000/status")
    assert r2.status_code == 200
    assert r2.json()["feedback_id"] == "550e8400-e29b-41d4-a716-446655440000"


def test_validate_feedback_basic():
    """Test basic feedback validation."""
    r = client.post(
        "/v1/feedback/validate",
        json={
            "user_id": "550e8400-e29b-41d4-a716-446655440000",
            "content": "nice",
            "recent_submissions_count": 0,
        },
    )
    assert r.status_code == 200
    assert r.json()["allow_submission"] is True
