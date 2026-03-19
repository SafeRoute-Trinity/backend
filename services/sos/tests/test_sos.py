import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import services.sos.main as sos_main
from services.sos.main import app, get_db


class FakeScalarsResult:
    def __init__(self, item):
        self._item = item

    def first(self):
        return self._item


class FakeExecuteResult:
    def __init__(self, item):
        self._item = item

    def scalars(self):
        return FakeScalarsResult(self._item)


class FakeDB:
    def __init__(self, *, trusted_contact=None, commit_raises: Exception | None = None):
        self.trusted_contact = trusted_contact or SimpleNamespace(phone="+353800000111")
        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.commit_raises = commit_raises

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        return FakeExecuteResult(self.trusted_contact)

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


class DummyAsyncClient:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        now = datetime.now(timezone.utc).isoformat()
        if url.endswith("/v1/notifications/sos/call"):
            return DummyResponse(
                {
                    "status": "initiated",
                    "call_id": "twilio_sid_123",
                    "timestamp": now,
                }
            )

        if url.endswith("/v1/notifications/sos/sms"):
            return DummyResponse(
                {
                    "emergency_id": json["sos_id"],
                    "status": "sent",
                    "sms_id": str(uuid.uuid4()),
                    "timestamp": now,
                    "message_sent": "Test SOS message",
                    "recipient": json["emergency_contact"]["phone"],
                }
            )

        return DummyResponse({}, status_code=404)


@pytest.fixture()
def fake_db():
    db = FakeDB()
    _override_db(db)
    yield db
    app.dependency_overrides.clear()
    sos_main.STATUS.clear()


@pytest.fixture()
def client(monkeypatch, fake_db):
    monkeypatch.setattr(sos_main.httpx, "AsyncClient", DummyAsyncClient)
    with TestClient(app) as test_client:
        yield test_client


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "sos"
    assert data["status"] == "running"


def test_emergency_call_success(client, fake_db):
    call_req = {
        "user_id": "auth0|calluser",
        "route_id": None,
        "lat": 53.34,
        "lon": -6.26,
        "trigger_type": "manual",
        "message": "Test emergency",
    }
    r = client.post("/v1/emergency/call", json=call_req)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "initiated"
    assert "call_id" in data
    assert "timestamp" in data
    assert "emergency_id" in data
    assert fake_db.committed is True


def test_emergency_call_missing_fields(client):
    payload = {"user_id": "missing-user"}
    r = client.post("/v1/emergency/call", json=payload)
    assert r.status_code == 422


def test_emergency_sms_success(client):
    sms_req = {
        "sos_id": str(uuid.uuid4()),
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
    assert data["status"] == "sent"
    assert data["recipient"] == "+353800000222"
    assert data["emergency_id"] == sms_req["sos_id"]


def test_emergency_sms_missing_fields(client):
    payload = {"sos_id": str(uuid.uuid4())}
    r = client.post("/v1/emergency/sms", json=payload)
    assert r.status_code == 422


def test_emergency_status_not_triggered(client):
    emergency_id = str(uuid.uuid4())
    r = client.get(f"/v1/emergency/{emergency_id}/status")
    assert r.status_code == 200
    data = r.json()
    assert data["emergency_id"] == emergency_id
    assert data["call_status"] == "not_triggered"
    assert data["sms_status"] == "not_sent"


def test_full_emergency_flow(client):
    call_req = {
        "user_id": "auth0|flowuser",
        "route_id": None,
        "lat": 53.34,
        "lon": -6.26,
        "trigger_type": "manual",
        "message": "Test emergency",
    }

    r1 = client.post("/v1/emergency/call", json=call_req)
    assert r1.status_code == 200
    emergency_id = str(r1.json()["emergency_id"])

    sms_req = {
        "sos_id": emergency_id,
        "user_id": "auth0|flowuser",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Bob", "phone": "+353800000333"},
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Bob"},
    }
    r2 = client.post("/v1/emergency/sms", json=sms_req)
    assert r2.status_code == 200

    r3 = client.get(f"/v1/emergency/{emergency_id}/status")
    assert r3.status_code == 200
    d = r3.json()
    assert d["emergency_id"] == emergency_id
    assert d["call_status"] == "initiated"
    assert d["sms_status"] == "sent"
