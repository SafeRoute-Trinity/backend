###pytest services/feedback/tests/test_main.py -q

from fastapi.testclient import TestClient
from services.feedback.main import app

client = TestClient(app)

def test_submit_and_status_feedback():
    req = {
        "feedback_id": "fbk_001",
        "user_id": "usr_demo",
        "type": "safety_issue",
        "description": "Broken street light",
        "severity": "high",
        "timestamp": "2025-11-07T10:00:00Z"
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200
    ticket = r.json()["ticket_number"]
    assert ticket.startswith("TKT-")

    r2 = client.get("/v1/feedback/fbk_001/status")
    assert r2.status_code == 200
    assert r2.json()["feedback_id"] == "fbk_001"

def test_validate_feedback():
    r = client.post("/v1/feedback/validate", json={
        "user_id": "usr_demo", "content": "nice", "recent_submissions_count": 0
    })
    assert r.status_code == 200
    assert r.json()["allow_submission"] is True

