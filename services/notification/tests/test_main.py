from fastapi.testclient import TestClient
from services.notification.main import app

client = TestClient(app)

def test_data_cleaner():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Hello FastAPI"}
