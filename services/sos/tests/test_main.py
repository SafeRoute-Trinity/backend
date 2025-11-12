#pytest -q
##或者只测试一个服务
#pytest services/sos/tests/test_main.py -q
#pip install pytest
#pip install pytest httpx requests


from fastapi.testclient import TestClient
from services.sos.main import app

client = TestClient(app)

def test_emergency_call_sms_status():
    call_req = {
        "sos_id": "SOS-TEST",
        "phone_number": "+112",
        "user_location": {"lat": 53.34, "lon": -6.26},
        "call_reason": "Test emergency"
    }
    r = client.post("/v1/emergency/call", json=call_req)
    assert r.status_code == 200
    assert r.json()["status"] == "initiated"

    sms_req = {
        "sos_id": "SOS-TEST",
        "recipient_phone": "+353800000222",
        "message": "Emergency! Need help.",
        "location_url": "https://maps.google.com/?q=53.34,-6.26"
    }
    r2 = client.post("/v1/emergency/sms", json=sms_req)
    assert r2.status_code == 200
    assert r2.json()["status"] == "sent"

    r3 = client.get("/v1/emergency/SOS-TEST/status")
    assert r3.status_code == 200
    d = r3.json()
    assert d["sos_id"] == "SOS-TEST"
    assert d["sms_status"] in ["sent", "not_sent"]
