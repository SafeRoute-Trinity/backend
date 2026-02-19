# pytest services/sos/tests/test_sos.py -q

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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


class DummyAsyncClientOK:
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


class DummyAsyncClientFail:
    def __init__(self, timeout=10.0):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        import httpx

        raise httpx.ConnectError("connect failed", request=SimpleNamespace(url=url))


# ----------------------------
# Tests
# ----------------------------
def test_emergency_call_success_200(client, monkeypatch):
    # Patch httpx.AsyncClient
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientOK)

    # Patch metrics + STATUS
    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    monkeypatch.setattr(sos, "SOS_CALLS_TOTAL", _Counter(), raising=False)
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

    # Response shape
    assert data["status"] == "initiated"
    assert data["call_id"] == "twilio_sid_123"
    assert "timestamp" in data

    # IMPORTANT: endpoint should include emergency_id in response
    assert "emergency_id" in data
    uuid.UUID(data["emergency_id"])

    # DB interactions
    assert fake_db.flushed is True
    assert fake_db.committed is True
    assert fake_db.rolled_back is False
    assert len(fake_db.added) >= 2  # Emergency + Audit

    # Metric incremented once
    assert sos.SOS_CALLS_TOTAL.count == 1


def test_emergency_call_notification_fail_503(client, monkeypatch):
    monkeypatch.setattr(sos.httpx, "AsyncClient", DummyAsyncClientFail)

    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    monkeypatch.setattr(sos, "SOS_CALLS_TOTAL", _Counter(), raising=False)
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

    # In failure branch you commit audit (per your code)
    assert fake_db.flushed is True
    assert fake_db.committed is True or fake_db.rolled_back is True
    assert len(fake_db.added) >= 2  # Emergency + Audit
    assert sos.SOS_CALLS_TOTAL.count == 0  # should NOT count success metric
