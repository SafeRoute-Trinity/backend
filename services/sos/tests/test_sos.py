# pytest -q
##或者只测试一个服务
# pytest services/sos/tests/test_main.py -q
# pip install pytest
# pip install pytest httpx requests

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


def test_emergency_call_sms_status(monkeypatch):
    monkeypatch.setattr(sos_main.httpx, "AsyncClient", _AsyncClientProxy)

    async def _sms_stub(self, payload):
        return {"status": "sent", "sid": "SMSTEST"}

    monkeypatch.setattr(notif_factory.SmsSender, "send", _sms_stub)

    call_req = {
        "sos_id": "SOS-TEST",
        "phone_number": "+112",
        "user_location": {"lat": 53.34, "lon": -6.26},
        "call_reason": "Test emergency",
    }
    r = client.post("/v1/emergency/call", json=call_req)
    assert r.status_code == 200
    assert r.json()["status"] == "initiated"

    sms_req = {
        "sos_id": "SOS-TEST",
        "user_id": "usr_demo",
        "location": {"lat": 53.34, "lon": -6.26},
        "emergency_contact": {"name": "Alice", "phone": "+353800000222"},
        "notification_type": "sos",
        "locale": "en",
        "variables": {"name": "Alice"},
    }
    r2 = client.post("/v1/emergency/sms", json=sms_req)
    assert r2.status_code == 200
    assert r2.json()["status"] in ["sent", "failed"]

    r3 = client.get("/v1/emergency/SOS-TEST/status")
    assert r3.status_code == 200
    d = r3.json()
    assert d["sos_id"] == "SOS-TEST"
    assert d["sms_status"] in ["sent", "failed", "not_sent"]
