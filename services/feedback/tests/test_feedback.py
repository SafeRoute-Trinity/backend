import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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

    async def get_feedbacks(
        self,
        db,
        user_id=None,
        status=None,
        feedback_type=None,
        skip=0,
        limit=10,
    ):
        """Return (total_count, list of row-like objects) for list_feedback."""
        items = list(self.fake_db.rows.values())
        if user_id is not None:
            items = [r for r in items if r.get("user_id") == user_id]
        if status is not None:
            status_val = status.value if hasattr(status, "value") else status
            items = [r for r in items if r.get("status") == status_val]
        if feedback_type is not None:
            type_val = feedback_type.value if hasattr(feedback_type, "value") else feedback_type
            items = [r for r in items if r.get("feedback_type") == type_val]
        items = sorted(items, key=lambda r: r.get("created_at") or "", reverse=True)
        total = len(items)
        page = items[skip : skip + limit]
        rows = [
            SimpleNamespace(
                feedback_id=v["feedback_id"],
                ticket_number=v["ticket_number"],
                status=v["status"],
                type=v.get("feedback_type"),
                severity=v.get("severity"),
                created_at=v["created_at"],
                updated_at=v["updated_at"],
            )
            for v in page
        ]
        return total, rows


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


# ---------- Coverage: submit branches (main.py submit) ----------
# - test_submit_feedback_empty_user_id_400: submit() ~404-405 (user_id required -> 400)
# - test_submit_feedback_auth0_prefix_ok: submit() ~408-411 (auth0| / auth0_ prefix stripping)
# - test_submit_feedback_db_failure_500: submit() ~467-470 (create_feedback/commit exception -> 500)


def test_submit_feedback_empty_user_id_400():
    """Empty user_id is rejected with 400 (handler check after Pydantic)."""
    r = client.post(
        "/v1/feedback/submit",
        json={
            "user_id": "",
            "type": "safety_issue",
            "description": "Test",
            "severity": "low",
        },
    )
    assert r.status_code == 400
    assert "user_id" in (r.json().get("detail") or "").lower()


def test_submit_feedback_auth0_prefix_ok():
    """Auth0-style user_id (auth0|id and auth0_id) is accepted and stripped."""
    for user_id in ["auth0|usr123", "auth0_usr456"]:
        r = client.post(
            "/v1/feedback/submit",
            json={
                "user_id": user_id,
                "type": "safety_issue",
                "description": "Auth0 user",
                "severity": "low",
            },
        )
        assert r.status_code == 200
        assert r.json().get("ticket_number", "").startswith("TKT-")


def test_submit_feedback_db_failure_500(monkeypatch):
    """DB/create_feedback failure returns 500 and rolls back."""

    class FailingFactory:
        async def create_feedback(self, *args, **kwargs):
            raise RuntimeError("db connection failed")

    monkeypatch.setattr(feedback_main, "get_feedback_factory", lambda: FailingFactory())
    r = client.post(
        "/v1/feedback/submit",
        json={
            "user_id": str(uuid.uuid4()),
            "type": "safety_issue",
            "description": "Test",
            "severity": "low",
        },
    )
    assert r.status_code == 500
    assert "Failed to submit feedback" in (r.json().get("detail") or "")


# ---------- Coverage: list_feedback (main.py list_feedback) ----------


def test_list_feedback_success():
    """GET /v1/feedback returns paginated list and uses get_feedbacks."""
    r = client.get("/v1/feedback?page=1&page_size=10")
    assert r.status_code == 200
    data = r.json()
    assert "data" in data
    assert "pagination" in data
    assert "filters" in data
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["page_size"] == 10
    assert data["pagination"]["total"] >= 0
    assert data["pagination"]["total_pages"] >= 0


def test_list_feedback_invalid_page_422():
    """page < 1 yields 422 (Query validation)."""
    r = client.get("/v1/feedback?page=0&page_size=10")
    assert r.status_code == 422


# ---------- Coverage: validate exception fallback (main.py validate ~602-612) ----------


def test_validate_feedback_spam_validator_exception_fallback(monkeypatch):
    """When spam validator raises, fallback to frequency check (recent_submissions_count > 10)."""

    class FailingValidator:
        async def validate(self, **kwargs):
            raise RuntimeError("spam service unavailable")

    class FailingValidatorFactory:
        def get_validator(self):
            return FailingValidator()

    monkeypatch.setattr(
        feedback_main,
        "get_spam_validator_factory",
        lambda: FailingValidatorFactory(),
    )
    r = client.post(
        "/v1/feedback/validate",
        json={
            "user_id": "usr1",
            "content": "some content",
            "recent_submissions_count": 15,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["allow_submission"] is False
    assert data["is_spam"] is True
    assert "high_frequency" in data["flags"]
    assert "Too many" in data["reason"]


# ---------- Coverage: system-feedback (main.py submit_system_feedback, verify_recaptcha, send_system_feedback_email) ----------


def test_system_feedback_privacy_not_accepted_400():
    """Privacy must be accepted for system feedback."""
    r = client.post(
        "/v1/system-feedback/submit",
        json={
            "user_id": "u",
            "content": "Feedback text",
            "privacy_accepted": False,
            "captcha_token": "token",
        },
    )
    assert r.status_code == 400
    assert "privacy" in (r.json().get("detail") or "").lower()


def test_system_feedback_success(monkeypatch):
    """System feedback success when captcha and email are mocked."""

    async def fake_verify(*args, **kwargs):
        return {"success": True}

    def fake_send_email(*args, **kwargs):
        pass

    monkeypatch.setattr(feedback_main, "verify_recaptcha", fake_verify)
    monkeypatch.setattr(feedback_main, "send_system_feedback_email", fake_send_email)
    r = client.post(
        "/v1/system-feedback/submit",
        json={
            "user_id": "u",
            "content": "Great app",
            "privacy_accepted": True,
            "captcha_token": "bypass",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "received"
    assert "success" in (data.get("message") or "").lower()


def test_system_feedback_captcha_failure_400(monkeypatch):
    """Captcha verification failure returns 400."""

    async def fake_verify_fail(*args, **kwargs):
        raise HTTPException(status_code=400, detail="Captcha verification failed")

    monkeypatch.setattr(feedback_main, "verify_recaptcha", fake_verify_fail)
    r = client.post(
        "/v1/system-feedback/submit",
        json={
            "user_id": "u",
            "content": "Feedback",
            "privacy_accepted": True,
            "captcha_token": "bad",
        },
    )
    assert r.status_code == 400
    assert "captcha" in (r.json().get("detail") or "").lower()


def test_system_feedback_email_failure_500(monkeypatch):
    """Email send failure returns 500."""

    async def fake_verify_ok(*args, **kwargs):
        return {"success": True}

    def fake_send_email_fail(*args, **kwargs):
        raise RuntimeError("SMTP unavailable")

    monkeypatch.setattr(feedback_main, "verify_recaptcha", fake_verify_ok)
    monkeypatch.setattr(feedback_main, "send_system_feedback_email", fake_send_email_fail)
    r = client.post(
        "/v1/system-feedback/submit",
        json={
            "user_id": "u",
            "content": "Feedback",
            "privacy_accepted": True,
            "captcha_token": "t",
        },
    )
    assert r.status_code == 500
    assert (
        "email" in (r.json().get("detail") or "").lower()
        or "feedback" in (r.json().get("detail") or "").lower()
    )
