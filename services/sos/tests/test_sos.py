# pytest services/sos/tests/test_sos.py -v

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import services.sos.main as sos_main
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


class DummyAsyncClientSmsOK:
    """Mock AsyncClient for SMS endpoint: returns success payload."""

    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        return DummyResponse(
            {
                "emergency_id": str(uuid.uuid4()),
                "status": "sent",
                "sms_id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message_sent": "test",
                "recipient": json.get("emergency_contact", {}).get("phone", ""),
            },
            status_code=200,
        )


_AsyncClientProxy = DummyAsyncClientCallOK


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
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "lat": 53.34,
        "lon": -6.26,
        "trigger_type": "manual",
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
    monkeypatch.setattr(sos_main.httpx, "AsyncClient", DummyAsyncClientSmsOK)

    sms_req = {
        "sos_id": "550e8400-e29b-41d4-a716-446655440002",
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
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
    # Use a valid UUID that was never used -> returns default not_triggered / not_sent
    r = client.get("/v1/emergency/550e8400-e29b-41d4-a716-446655440099/status")
    assert r.status_code == 200
    data = r.json()
    assert data["emergency_id"] == "550e8400-e29b-41d4-a716-446655440099"
    assert data["call_status"] in ["not_triggered", "initiated", "connected", "failed"]
    assert data["sms_status"] in ["not_sent", "sent", "failed"]


# ----------------------------
# Full flow: call → sms → status
# ----------------------------
def test_full_emergency_flow(monkeypatch):
    # Mock that handles both call and SMS POSTs
    class _CombinedClient(DummyAsyncClientCallOK):
        async def post(self, url, json_payload=None):
            json_payload = json_payload or {}
            if "emergency_contact" in json_payload:
                return await DummyAsyncClientSmsOK(timeout=self.timeout).post(url, json_payload)
            return await DummyAsyncClientCallOK.post(self, url, json_payload)

    monkeypatch.setattr(sos_main.httpx, "AsyncClient", _CombinedClient)

    call_req = {
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "lat": 53.34,
        "lon": -6.26,
        "trigger_type": "manual",
    }
    sms_req = {
        "sos_id": "550e8400-e29b-41d4-a716-446655440001",
        "user_id": "550e8400-e29b-41d4-a716-446655440000",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Bob", "phone": "+353800000333"},
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Bob"},
    }

    r1 = client.post("/v1/emergency/call", json=call_req)
    assert r1.status_code == 200
    emergency_id = str(r1.json()["emergency_id"])
    r2 = client.post("/v1/emergency/sms", json=sms_req)
    assert r2.status_code == 200
    r3 = client.get(f"/v1/emergency/{emergency_id}/status")
    assert r3.status_code == 200
    d = r3.json()
    assert d["emergency_id"] == emergency_id
    assert d["sms_status"] in ["sent", "failed", "not_sent"]
