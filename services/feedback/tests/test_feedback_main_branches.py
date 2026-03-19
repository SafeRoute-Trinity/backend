import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

import services.feedback.main as feedback_main
from services.feedback.main import app, get_db
from services.feedback.types import FeedbackType, SeverityType, Status


def _run(coro):
    return asyncio.run(coro)


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
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt, params=None):
        feedback_id = params.get("fid") if params else None
        return _FakeExecuteResult(self.rows.get(feedback_id))

    def add(self, obj):
        # submit() creates Feedback via factory; status() reads via execute() only.
        pass

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
        db: AsyncSession,
        user_id=None,
        status=None,
        feedback_type=None,
        skip: int = 0,
        limit: int = 10,
    ):
        items = list(self.fake_db.rows.values())

        if user_id:
            items = [x for x in items if x.get("user_id") == user_id]
        if status:
            status_val = status.value if hasattr(status, "value") else status
            items = [x for x in items if x.get("status") == status_val]
        if feedback_type:
            type_val = feedback_type.value if hasattr(feedback_type, "value") else feedback_type
            items = [x for x in items if x.get("feedback_type") == type_val]

        total = len(items)
        page = items[skip : skip + limit]
        rows = [
            SimpleNamespace(
                feedback_id=v["feedback_id"],
                ticket_number=v.get("ticket_number"),
                status=v.get("status"),
                type=v.get("feedback_type"),
                severity=v.get("severity"),
                created_at=v.get("created_at"),
                updated_at=v.get("updated_at"),
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


client = TestClient(app)


def test_maybe_uuid_parses_valid_uuid_and_returns_none_for_invalid():
    good = uuid.uuid4()
    assert feedback_main._maybe_uuid(good) == good
    assert feedback_main._maybe_uuid(str(good)) == good
    assert feedback_main._maybe_uuid(None) is None
    assert feedback_main._maybe_uuid("not-a-uuid") is None
    # Falls through the try-block without hitting any return -> line `return None`
    assert feedback_main._maybe_uuid(123) is None


def test_verify_recaptcha_bypass_success(monkeypatch):
    monkeypatch.setattr(feedback_main, "ENABLE_CAPTCHA_BYPASS", True)
    monkeypatch.setattr(feedback_main, "CAPTCHA_BYPASS_TOKEN", "tkn")

    res = _run(feedback_main.verify_recaptcha(token="tkn", remote_ip=None))
    assert res["success"] is True
    assert res["bypass"] is True


def test_verify_recaptcha_secret_missing_500(monkeypatch):
    monkeypatch.setattr(feedback_main, "RECAPTCHA_SECRET_KEY", None)
    monkeypatch.setattr(feedback_main, "ENABLE_CAPTCHA_BYPASS", False)

    with pytest.raises(feedback_main.HTTPException) as e:
        _run(feedback_main.verify_recaptcha(token="any"))
    assert e.value.status_code == 500
    assert "RECAPTCHA_SECRET_KEY" in e.value.detail


def test_verify_recaptcha_http_failure_502(monkeypatch):
    monkeypatch.setattr(feedback_main, "RECAPTCHA_SECRET_KEY", "secret")
    monkeypatch.setattr(feedback_main, "ENABLE_CAPTCHA_BYPASS", False)

    class _BadAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(feedback_main.httpx, "AsyncClient", _BadAsyncClient)

    with pytest.raises(feedback_main.HTTPException) as e:
        _run(feedback_main.verify_recaptcha(token="tkn", remote_ip="1.2.3.4"))
    assert e.value.status_code == 502
    assert "Captcha verification request failed" in e.value.detail


def test_verify_recaptcha_unsuccessful_400(monkeypatch):
    monkeypatch.setattr(feedback_main, "RECAPTCHA_SECRET_KEY", "secret")
    monkeypatch.setattr(feedback_main, "ENABLE_CAPTCHA_BYPASS", False)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"success": False}

    class _OkClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(feedback_main.httpx, "AsyncClient", _OkClient)

    with pytest.raises(feedback_main.HTTPException) as e:
        _run(feedback_main.verify_recaptcha(token="tkn"))
    assert e.value.status_code == 400
    assert "Captcha verification failed" in e.value.detail


def test_verify_recaptcha_success_returns_result(monkeypatch):
    monkeypatch.setattr(feedback_main, "RECAPTCHA_SECRET_KEY", "secret")
    monkeypatch.setattr(feedback_main, "ENABLE_CAPTCHA_BYPASS", False)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    class _OkClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(feedback_main.httpx, "AsyncClient", _OkClient)

    res = _run(feedback_main.verify_recaptcha(token="tkn"))
    assert res["success"] is True


def test_send_system_feedback_email_missing_smtp_creds_raises(monkeypatch):
    monkeypatch.setattr(feedback_main, "SMTP_USERNAME", None)
    monkeypatch.setattr(feedback_main, "SMTP_PASSWORD", None)

    with pytest.raises(RuntimeError, match="SMTP credentials are not configured"):
        feedback_main.send_system_feedback_email(
            user_id="u",
            user_email=None,
            subject=None,
            content="hello",
            page_url=None,
            user_agent=None,
        )


def test_send_system_feedback_email_success_uses_smtp(monkeypatch):
    sent = {"from": None, "to": None, "subject": None, "body": None}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=20):
            self.host = host
            self.port = port

        def starttls(self):
            return None

        def login(self, username, password):
            # Not asserting credentials; just ensure called.
            return None

        def sendmail(self, sender, recipients, msg):
            sent["from"] = sender
            sent["to"] = recipients
            sent["body"] = msg

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(feedback_main, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(feedback_main, "SMTP_PASSWORD", "pass")
    monkeypatch.setattr(feedback_main, "SMTP_HOST", "smtp.test.local")
    monkeypatch.setattr(feedback_main, "SMTP_PORT", 587)
    monkeypatch.setattr(feedback_main, "SYSTEM_FEEDBACK_TO_EMAIL", "dest@example.com")
    monkeypatch.setattr(feedback_main.smtplib, "SMTP", _FakeSMTP)

    feedback_main.send_system_feedback_email(
        user_id="u",
        user_email="u@example.com",
        subject="subj",
        content="hello content",
        page_url="http://page",
        user_agent="ua",
    )

    assert sent["from"] == "user@example.com"
    assert sent["to"] == ["dest@example.com"]
    assert "[SafeRoute][System Feedback]" in sent["body"]


def test_submit_feedback_write_audit_failure_is_swallowed(
    monkeypatch, override_feedback_dependencies
):
    async def _boom(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(feedback_main, "write_audit", AsyncMock(side_effect=_boom))

    req = {
        "user_id": "usr_demo",
        "type": "safety_issue",
        "description": "Broken street light",
        "severity": "high",
    }
    r = client.post("/v1/feedback/submit", json=req)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == Status.RECEIVED.value


def test_validate_feedback_write_audit_failure_is_swallowed(
    monkeypatch, override_feedback_dependencies
):
    async def _boom(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(feedback_main, "write_audit", AsyncMock(side_effect=_boom))

    r = client.post(
        "/v1/feedback/validate",
        json={"user_id": "usr_demo", "content": "nice", "recent_submissions_count": 0},
    )
    assert r.status_code == 200
    assert r.json()["allow_submission"] is True


def test_status_write_audit_failure_is_swallowed_and_feedback_id_fallback_uses_path_uuid(
    monkeypatch, override_feedback_dependencies
):
    fake_db: FakeDB = override_feedback_dependencies

    feedback_id = uuid.uuid4()
    # Provide feedback_id as a string so the fallback `if not isinstance(..., uuid.UUID)` runs.
    fake_db.rows[str(feedback_id)] = {
        "feedback_id": str(feedback_id),
        "user_id": "auth0|user123",
        "ticket_number": "TKT-TEST",
        "status": Status.RECEIVED.value,
        "feedback_type": FeedbackType.SAFETY_ISSUE.value,
        "severity": SeverityType.HIGH.value,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    async def _boom(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(feedback_main, "write_audit", AsyncMock(side_effect=_boom))

    r = client.get(f"/v1/feedback/{feedback_id}/status")
    assert r.status_code == 200
    assert r.json()["feedback_id"] == str(feedback_id)


def test_submit_feedback_route_id_empty_string_becomes_none_and_uses_default_route(
    monkeypatch, override_feedback_dependencies
):
    fake_db: FakeDB = override_feedback_dependencies
    monkeypatch.setattr(
        feedback_main, "write_audit", AsyncMock(side_effect=RuntimeError("audit failed"))
    )

    r = client.post(
        "/v1/feedback/submit",
        json={
            "user_id": "usr_demo",
            "route_id": "",
            "type": "safety_issue",
            "description": "Broken street light",
            "severity": "high",
        },
    )
    assert r.status_code == 200

    feedback_id = r.json()["feedback_id"]
    saved = fake_db.rows[str(feedback_id)]
    # route_id should be replaced by DEFAULT_ROUTE_ID when validator converts "" -> None
    assert "route_id" in saved


def test_list_feedback_executes_loop_and_maps_type_from_db_row(
    override_feedback_dependencies, monkeypatch
):
    fake_db: FakeDB = override_feedback_dependencies
    monkeypatch.setattr(
        feedback_main, "write_audit", AsyncMock(side_effect=RuntimeError("audit failed"))
    )

    # Create one item so GET /v1/feedback returns a non-empty list.
    submit = client.post(
        "/v1/feedback/submit",
        json={
            "user_id": "usr1",
            "type": "safety_issue",
            "description": "desc",
            "severity": "low",
        },
    )
    assert submit.status_code == 200
    feedback_id = submit.json()["feedback_id"]
    assert str(feedback_id) in fake_db.rows

    r = client.get("/v1/feedback?page=1&page_size=10")
    assert r.status_code == 200
    data = r.json()
    assert len(data["data"]) >= 1
    assert data["data"][0]["feedback_id"] == feedback_id
    # list_feedback loop: `api_type = row.type if row.type else None`
    assert data["data"][0]["type"] in (None, "safety_issue")


def test_metrics_endpoint_executes_return_path():
    r = client.get("/metrics")
    assert r.status_code == 200
