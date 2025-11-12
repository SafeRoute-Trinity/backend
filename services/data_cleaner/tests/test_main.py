###pytest services/data_cleaner/tests/test_main.py -q


from fastapi.testclient import TestClient
from services.data_cleaner.main import app

client = TestClient(app)

def test_collect_and_sanitize():
    collect = {
        "source": "crime_stats",
        "data_type": "incidents",
        "batch_id": "batch_01",
        "records": [{"id": 1, "x": 1}, {"id": 2, "x": None}],
        "collected_at": "2025-11-07T10:00:00Z"
    }
    r = client.post("/v1/data/collect", json=collect)
    assert r.status_code == 200
    assert r.json()["status"] == "processing"

    sanitize = {"batch_id": "batch_01", "rules": ["remove_null_values","mask_personal_identifiers"]}
    r2 = client.post("/v1/data/sanitize", json=sanitize)
    assert r2.status_code == 200
    d = r2.json()
    assert d["status"] == "completed"
    assert "summary" in d

def test_audit_log_and_query():
    log = {
        "event_id": "e1",
        "event_type": "system_event",
        "component": "DataCleaner",
        "action": "SanitizeBatch",
        "timestamp": "2025-11-07T10:01:00Z",
        "severity": "info",
        "metadata": {"batch_id": "batch_01"}
    }
    r = client.post("/v1/audit/log", json=log)
    assert r.status_code == 200
    log_id = r.json()["log_id"]

    query = {
        "date_from": "2025-11-07T00:00:00Z",
        "date_to": "2025-11-08T00:00:00Z",
        "limit": 10, "offset": 0
    }
    r2 = client.post("/v1/audit/query", json=query)
    assert r2.status_code == 200
    data = r2.json()
    assert data["total_results"] >= 1
    assert any(item["log_id"] == log_id for item in data["logs"])
