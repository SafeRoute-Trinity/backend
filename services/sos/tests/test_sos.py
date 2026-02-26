# pytest services/sos/tests/test_sos.py -v

import httpx
from fastapi.testclient import TestClient
from httpx import ASGITransport

from services.notification import factory as notif_factory
from services.notification.main import app as notification_app
from services.sos import main as sos_main

client = TestClient(sos_main.app)
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _AsyncClientProxy:
    def __init__(self, *args, **kwargs) -> None:
        transport = ASGITransport(app=notification_app)
        self._client = _REAL_ASYNC_CLIENT(transport=transport, base_url="http://testserver")

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        await self._client.aclose()


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

    async def _sms_stub(self, payload):
        return {"status": "sent", "sid": "SMSTEST"}

    monkeypatch.setattr(notif_factory.SmsSender, "send", _sms_stub)

    call_req = {
        "sos_id": "SOS-FLOW",
        "phone_number": "+112",
        "user_location": {"lat": 53.34, "lon": -6.26},
        "call_reason": "Test emergency",
    }
    r = client.post("/v1/emergency/call", json=call_req)
    assert r.status_code == 200
    assert r.json()["status"] == "initiated"

    sms_req = {
        "sos_id": "SOS-FLOW",
        "user_id": "auth0|flowuser",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Bob", "phone": "+353800000333"},
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Bob"},
    }
    r2 = client.post("/v1/emergency/sms", json=sms_req)
    assert r2.status_code == 200
    assert r2.json()["status"] in ["sent", "failed"]

    r3 = client.get("/v1/emergency/SOS-FLOW/status")
    assert r3.status_code == 200
    d = r3.json()
    assert d["sos_id"] == "SOS-FLOW"
    assert d["sms_status"] in ["sent", "failed", "not_sent"]
