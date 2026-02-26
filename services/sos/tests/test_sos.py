# pytest services/sos/tests/test_sos.py -v

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


# ----------------------------
# Root / health
# ----------------------------
def test_root():
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "sos"
    assert data["status"] == "running"


# ----------------------------
# Emergency call
# ----------------------------
def test_emergency_call_success(monkeypatch):
    monkeypatch.setattr(sos_main.httpx, "AsyncClient", _AsyncClientProxy)

    call_req = {
        "sos_id": "SOS-CALL-001",
        "phone_number": "+112",
        "user_location": {"lat": 53.34, "lon": -6.26},
        "call_reason": "Test emergency",
    }
    r = client.post("/v1/emergency/call", json=call_req)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "initiated"
    assert "call_id" in data
    assert "timestamp" in data


def test_emergency_call_missing_fields():
    payload = {"sos_id": "SOS-MISSING"}
    r = client.post("/v1/emergency/call", json=payload)
    assert r.status_code == 422


# ----------------------------
# Emergency SMS
# ----------------------------
def test_emergency_sms_success(monkeypatch):
    monkeypatch.setattr(sos_main.httpx, "AsyncClient", _AsyncClientProxy)

    async def _sms_stub(self, payload):
        return {"status": "sent", "sid": "SMSTEST"}

    monkeypatch.setattr(notif_factory.SmsSender, "send", _sms_stub)

    sms_req = {
        "sos_id": "SOS-SMS-001",
        "user_id": "auth0|smsuser",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Alice", "phone": "+353800000222"},
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Alice"},
    }
    r = client.post("/v1/emergency/sms", json=sms_req)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ["sent", "failed"]


def test_emergency_sms_missing_fields():
    payload = {"sos_id": "SOS-INCOMPLETE"}
    r = client.post("/v1/emergency/sms", json=payload)
    assert r.status_code == 422


# ----------------------------
# Status check
# ----------------------------
def test_emergency_status_not_triggered():
    r = client.get("/v1/emergency/SOS-NEVER-EXISTED/status")
    assert r.status_code == 200
    data = r.json()
    assert data["sos_id"] == "SOS-NEVER-EXISTED"
    assert data["call_status"] in ["not_triggered", "initiated", "connected", "failed"]
    assert data["sms_status"] in ["not_sent", "sent", "failed"]


# ----------------------------
# Full flow: call → sms → status
# ----------------------------
def test_full_emergency_flow(monkeypatch):
    monkeypatch.setattr(sos_main.httpx, "AsyncClient", _AsyncClientProxy)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    call_req = {
        "sos_id": "SOS-FLOW",
        "phone_number": "+112",
        "user_location": {"lat": 53.34, "lon": -6.26},
        "call_reason": "Test emergency",
    }

    sms_req = {
        "sos_id": "SOS-FLOW",
        "user_id": "auth0|flowuser",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Bob", "phone": "+353800000333"},
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Bob"},
    }

    r3 = client.get("/v1/emergency/SOS-FLOW/status")
    assert r3.status_code == 200
    d = r3.json()
    assert d["sos_id"] == "SOS-FLOW"
    assert d["sms_status"] in ["sent", "failed", "not_sent"]
