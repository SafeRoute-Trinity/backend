import uuid

import pytest
from fastapi.testclient import TestClient

import services.feedback.main as feedback_main
from services.feedback.main import app, get_db

client = TestClient(app)


class _FakeMappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeExecuteResult:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        return _FakeMappings(self._row)


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt, params=None):
        feedback_id = params.get("fid") if params else None
        return _FakeExecuteResult(self.rows.get(feedback_id))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class FakeFeedbackFactory:
    def __init__(self, fake_db: FakeDB):
        self.fake_db = fake_db

    async def create_feedback(
        self,
        *,
        feedback_id,
        user_id,
        ticket_number,
        route_id,
        type,
        severity,
        created_at,
        status,
        **kwargs,
    ):
        self.fake_db.rows[str(feedback_id)] = {
            "feedback_id": feedback_id,
            "user_id": user_id,
            "ticket_number": ticket_number,
            "status": status.value if hasattr(status, "value") else status,
            "feedback_type": type.value if hasattr(type, "value") else type,
            "severity": severity.value if hasattr(severity, "value") else severity,
            "created_at": created_at,
            "updated_at": created_at,
            "route_id": route_id,
            **kwargs,
        }


@pytest.fixture(autouse=True)
def override_feedback_dependencies(monkeypatch):
    fake_db = FakeDB()

    async def _override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _override_get_db
    monkeypatch.setattr(feedback_main, "get_feedback_factory", lambda: FakeFeedbackFactory(fake_db))
    yield fake_db
    app.dependency_overrides.clear()


def test_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "feedback"
    assert data["status"] == "running"


def test_submit_and_status_feedback():
    req = {
        "user_id": "usr_demo",
        "type": "safety_issue",
        "description": "Broken street light",
        "severity": "high",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200
    payload = r.json()
    assert payload["ticket_number"].startswith("TKT-")

    feedback_id = payload["feedback_id"]
    r2 = client.get(f"/v1/feedback/{feedback_id}/status")
    assert r2.status_code == 200
    assert r2.json()["feedback_id"] == feedback_id


def test_validate_feedback():
    r = client.post(
        "/v1/feedback/validate",
        json={"user_id": "usr_demo", "content": "nice", "recent_submissions_count": 0},
    )
    assert r.status_code == 200
    assert r.json()["allow_submission"] is True


def test_submit_feedback_different_types():
    for feedback_type in ["safety_issue", "route_quality", "others"]:
        req = {
            "user_id": str(uuid.uuid4()),
            "type": feedback_type,
            "description": f"Test feedback for {feedback_type}",
            "severity": "medium",
        }
        r = client.post("/v1/feedback/submit", json=req)
        assert r.status_code == 200
        assert "ticket_number" in r.json()


def test_submit_feedback_different_severities():
    for severity in ["low", "medium", "high", "critical"]:
        req = {
            "user_id": str(uuid.uuid4()),
            "type": "safety_issue",
            "description": f"Test {severity} severity feedback",
            "severity": severity,
        }
        r = client.post("/v1/feedback/submit", json=req)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "received"


def test_submit_feedback_with_location():
    req = {
        "user_id": str(uuid.uuid4()),
        "type": "safety_issue",
        "location": {"lat": 53.3498, "lon": -6.2603},
        "description": "Pothole at this location",
        "severity": "high",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200


def test_submit_feedback_with_route():
    req = {
        "user_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "type": "route_quality",
        "description": "Route took me through unsafe area",
        "severity": "high",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200


def test_submit_feedback_missing_required_fields():
    r = client.post("/v1/feedback/submit", json={})
    assert r.status_code == 422


def test_submit_feedback_invalid_type():
    req = {
        "user_id": str(uuid.uuid4()),
        "type": "invalid_type",
        "description": "Test",
        "severity": "low",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 422


def test_submit_feedback_invalid_severity():
    req = {
        "user_id": str(uuid.uuid4()),
        "type": "safety_issue",
        "description": "Test",
        "severity": "super_critical",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 422


def test_validate_feedback_spam_detection():
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
    assert "flags" in data


def test_validate_feedback_missing_fields():
    r = client.post(
        "/v1/feedback/validate",
        json={
            "user_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 422


def test_get_feedback_status_nonexistent():
    fake_id = str(uuid.uuid4())
    r = client.get(f"/v1/feedback/{fake_id}/status")
    assert r.status_code == 404


def test_get_feedback_status_multiple_times():
    req = {
        "user_id": str(uuid.uuid4()),
        "type": "others",
        "description": "Testing multiple status checks",
        "severity": "low",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200
    feedback_id = r.json()["feedback_id"]

    for _ in range(3):
        r2 = client.get(f"/v1/feedback/{feedback_id}/status")
        assert r2.status_code == 200
        assert r2.json()["feedback_id"] == feedback_id


def test_submit_feedback_with_attachments():
    req = {
        "user_id": str(uuid.uuid4()),
        "type": "safety_issue",
        "description": "Photo evidence of broken light",
        "severity": "high",
        "attachments": [
            "https://example.com/photo1.jpg",
            "https://example.com/photo2.jpg",
        ],
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200


def test_metrics_endpoint():
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
