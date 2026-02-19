# pytest services/sos/tests/test_sos.py -q

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import services.sos.main as sos
from services.sos.main import app, get_db


# ----------------------------
# Fake db session (async)
# ----------------------------
class FakeDB:
    def __init__(self, *, commit_raises: Exception | None = None):
        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.commit_raises = commit_raises

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        if self.commit_raises:
            raise self.commit_raises
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _override_db(fake_db: FakeDB):
    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db


@pytest.fixture()
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------
# Metrics mock
# ----------------------------
class Counter:
    def __init__(self):
        self.count = 0

    def inc(self):
        self.count += 1


# ----------------------------
# httpx AsyncClient mocks
# ----------------------------
class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "boom",
                request=SimpleNamespace(url="http://test"),
                response=SimpleNamespace(status_code=self.status_code),
            )

    def json(self):
        return self._payload


class DummyAsyncClientCallOK:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        return DummyResponse(
            {
                "status": "initiated",
                "call_id": "twilio_sid_123",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            status_code=200,
        )


class DummyAsyncClientCallFail:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        import httpx

        raise httpx.ConnectError("connect failed", request=SimpleNamespace(url=url))


class DummyAsyncClientSMSOK:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        # EmergencySMSResponse wants: emergency_id, status, sms_id, timestamp, message_sent, recipient
        return DummyResponse(
            {
                "emergency_id": str(uuid.uuid4()),
                "status": "sent",
                "sms_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message_sent": "hello",
                "recipient": "+353123456789",
            },
            status_code=200,
        )


class DummyAsyncClientSMSFailStatus500:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        return DummyResponse({"detail": "downstream error"}, status_code=500)


class DummyAsyncClientTestSMSOK:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        return DummyResponse(
            {"status": "sent", "sid": "SMxxxx", "to": json["to_phone"], "message": json["message"]},
            status_code=200,
        )


class DummyAsyncClientTestSMSFail:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        import httpx

        raise httpx.ConnectError("boom", request=SimpleNamespace(url=url))


# ----------------------------
# Tests: /v1/emergency/call
# ----------------------------
def test_emergency_call_success_200_writes_status_and_metric(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientCallOK)

    counter_calls = Counter()
    monkeypatch.setattr(sos, "SOS_CALLS_TOTAL", counter_calls, raising=False)

    monkeypatch.setattr(sos, "STATUS", {}, raising=False)

    fake_db = FakeDB()
    _override_db(fake_db)

    payload = {
        "user_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "lat": 53.3498,
        "lon": -6.2603,
        "trigger_type": "manual",
        "message": "help",
    }

    res = client.post("/v1/emergency/call", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "initiated"
    assert data["call_id"] == "twilio_sid_123"
    assert "timestamp" in data
    assert "emergency_id" in data

    eid = uuid.UUID(data["emergency_id"])
    assert eid in sos.STATUS  # ✅ status dict updated
    assert sos.STATUS[eid]["call_status"] == "initiated"
    assert sos.STATUS[eid]["sms_status"] == "not_sent"

    assert fake_db.flushed is True
    assert fake_db.committed is True
    assert fake_db.rolled_back is False
    assert len(fake_db.added) >= 2  # Emergency + Audit

    assert counter_calls.count == 1


def test_emergency_call_notification_fail_503(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientCallFail)

    counter_calls = Counter()
    monkeypatch.setattr(sos, "SOS_CALLS_TOTAL", counter_calls, raising=False)

    monkeypatch.setattr(sos, "STATUS", {}, raising=False)

    fake_db = FakeDB()
    _override_db(fake_db)

    payload = {
        "user_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "lat": 0.0,
        "lon": 0.0,
        "trigger_type": "automatic",
        "message": "test",
    }

    res = client.post("/v1/emergency/call", json=payload)
    assert res.status_code == 503, res.text

    assert fake_db.flushed is True
    assert fake_db.committed is True or fake_db.rolled_back is True
    assert len(fake_db.added) >= 2  # Emergency + Audit
    assert counter_calls.count == 0  # metric should not increment on failure
    assert sos.STATUS == {}  # no status recorded on failure


def test_emergency_call_commit_integrity_error_400(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientCallOK)
    monkeypatch.setattr(sos, "SOS_CALLS_TOTAL", Counter(), raising=False)
    monkeypatch.setattr(sos, "STATUS", {}, raising=False)

    fake_db = FakeDB(commit_raises=IntegrityError("stmt", "params", Exception("orig")))
    _override_db(fake_db)

    payload = {
        "user_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "lat": 1.0,
        "lon": 2.0,
        "trigger_type": "manual",
        "message": "x",
    }

    res = client.post("/v1/emergency/call", json=payload)
    assert res.status_code == 400, res.text
    assert fake_db.rolled_back is True


# ----------------------------
# Tests: /v1/emergency/{id}/status
# ----------------------------
def test_get_status_invalid_uuid_400(client):
    res = client.get("/v1/emergency/not-a-uuid/status")
    assert res.status_code == 400
    assert res.json()["detail"] == "emergency_id must be a valid UUID"


def test_get_status_not_found_returns_default(client, monkeypatch):
    monkeypatch.setattr(sos, "STATUS", {}, raising=False)

    eid = uuid.uuid4()
    res = client.get(f"/v1/emergency/{eid}/status")
    assert res.status_code == 200, res.text
    data = res.json()

    # Current implementation returns default
    assert data["call_status"] == "not_triggered"
    assert data["sms_status"] == "not_sent"


def test_get_status_found_returns_saved(client, monkeypatch):
    eid = uuid.uuid4()
    monkeypatch.setattr(
        sos,
        "STATUS",
        {
            str(eid): {
                "emergency_id": str(eid),
                "call_status": "initiated",
                "sms_status": "not_sent",
                "last_update": datetime.now(timezone.utc).isoformat(),
            }
        },
        raising=False,
    )

    res = client.get(f"/v1/emergency/{eid}/status")
    assert res.status_code == 200
    data = res.json()
    assert data["call_status"] == "initiated"
    assert data["sms_status"] == "not_sent"


# ----------------------------
# Tests: /v1/emergency/sms
# ----------------------------
def test_emergency_sms_invalid_user_id_400(client):
    payload = {
        "user_id": "not-a-uuid",
        "location": {"lat": 1.0, "lon": 2.0},
        "emergency_contact": {"name": "A", "phone": "+353123"},
        "variables": {"x": "y"},
    }
    res = client.post("/v1/emergency/sms", json=payload)
    # Pydantic already rejects invalid uuid with 422; if it passes, endpoint rejects with 400
    assert res.status_code in (400, 422)


def test_emergency_sms_success_200_increments_metric(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientSMSOK)

    counter_sms = Counter()
    monkeypatch.setattr(sos, "SOS_SMS_TOTAL", counter_sms, raising=False)

    # Avoid real write_audit side effects
    async def _noop_write_audit(**kwargs):
        return None

    monkeypatch.setattr(sos, "write_audit", _noop_write_audit, raising=False)

    monkeypatch.setattr(sos, "STATUS", {}, raising=False)

    fake_db = FakeDB()
    _override_db(fake_db)

    payload = {
        "user_id": str(uuid.uuid4()),
        "location": {"lat": 1.0, "lon": 2.0},
        "emergency_contact": {"name": "A", "phone": "+353123"},
        "variables": {"x": "y"},
    }

    res = client.post("/v1/emergency/sms", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "sent"
    assert "sms_id" in data
    assert "recipient" in data

    assert counter_sms.count == 1


def test_emergency_sms_downstream_500_returns_503(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientSMSFailStatus500)

    async def _noop_write_audit(**kwargs):
        return None

    monkeypatch.setattr(sos, "write_audit", _noop_write_audit, raising=False)

    fake_db = FakeDB()
    _override_db(fake_db)

    payload = {
        "user_id": str(uuid.uuid4()),
        "location": {"lat": 1.0, "lon": 2.0},
        "emergency_contact": {"name": "A", "phone": "+353123"},
        "variables": {"x": "y"},
    }

    res = client.post("/v1/emergency/sms", json=payload)
    assert res.status_code == 503, res.text


# ----------------------------
# Tests: /v1/test/sms
# ----------------------------
def test_test_sms_success_200(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientTestSMSOK)

    async def _noop_write_audit(**kwargs):
        return None

    monkeypatch.setattr(sos, "write_audit", _noop_write_audit, raising=False)

    fake_db = FakeDB()
    _override_db(fake_db)

    payload = {"to_phone": "+353123", "message": "hi"}
    res = client.post("/v1/test/sms", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "sent"
    assert data["to"] == "+353123"
    assert data["message"] == "hi"


def test_test_sms_fail_503(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientTestSMSFail)

    async def _noop_write_audit(**kwargs):
        return None

    monkeypatch.setattr(sos, "write_audit", _noop_write_audit, raising=False)

    fake_db = FakeDB()
    _override_db(fake_db)

    payload = {"to_phone": "+353123", "message": "hi"}
    res = client.post("/v1/test/sms", json=payload)
    assert res.status_code == 503, res.text
