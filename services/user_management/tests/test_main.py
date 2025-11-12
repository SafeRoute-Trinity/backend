#pytest services/user_management/tests/test_main.py -q
#pytest services/user_management/tests/test_main.py -k test_register_user -q


from fastapi.testclient import TestClient
from services.user_management.main import app
from datetime import datetime
import re

client = TestClient(app)

# ------------------------------------------------------
# 1. Test register API (post)
# ------------------------------------------------------
def test_register_user():
    payload = {
        "email": "testuser@example.com",
        "password_hash": "hash123",
        "device_id": "dev_001",
        "phone": "+353123456789",
        "name": "Test User"
    }

    response = client.post("/v1/users/register", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "created"
    assert data["email"] == payload["email"]
    assert data["device_id"] == payload["device_id"]
    assert "user_id" in data
    assert re.match(r"usr_[a-f0-9]{8}", data["user_id"])
    assert data["auth"]["token"].startswith("atk_")

# ------------------------------------------------------
# 2. Test login API (post)
# ------------------------------------------------------
def test_login_user():
    payload = {
        "email": "testuser@example.com",
        "password_hash": "hash123",
        "device_id": "dev_001"
    }

    response = client.post("/v1/auth/login", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "authenticated"
    assert data["email"] == payload["email"]
    assert "user_id" in data
    assert "auth" in data
    assert data["auth"]["token"].startswith("atk_")

# ------------------------------------------------------
# 3. Test save preferences API (post)
# ------------------------------------------------------
def test_save_preferences():
    # 先登入拿 user_id
    login = client.post("/v1/auth/login", json={
        "email": "pref@example.com",
        "password_hash": "hashx",
        "device_id": "dev_002"
    }).json()
    user_id = login["user_id"]

    payload = {
        "voice_guidance": "on",
        "safety_bias": "safest",
        "units": "metric"
    }

    response = client.post(f"/v1/users/{user_id}/preferences", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "preferences_saved"
    assert data["preferences"]["voice_guidance"] == "on"
    assert data["preferences"]["safety_bias"] == "safest"
    assert "updated_at" in data

# ------------------------------------------------------
# 4. Test upsert trusted contact API (post)
# ------------------------------------------------------
def test_upsert_trusted_contact():
    # 先登入拿 user_id
    login = client.post("/v1/auth/login", json={
        "email": "trusted@example.com",
        "password_hash": "hashz",
        "device_id": "dev_003"
    }).json()
    user_id = login["user_id"]

    payload = {
        "name": "Alice",
        "phone": "+353800000111",
        "relationship": "friend",
        "is_primary": True
    }

    response = client.post(f"/v1/users/{user_id}/trusted-contacts", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "contact_upserted"
    assert data["contact"]["name"] == "Alice"
    assert data["contact"]["phone"] == "+353800000111"
    assert re.match(r"ctc_[a-f0-9]{6}", data["contact_id"])

# ------------------------------------------------------
# 5. Test get user API (post)
# ------------------------------------------------------
def test_get_user_info():
    # 先建立 user
    reg = client.post("/v1/users/register", json={
        "email": "info@example.com",
        "password_hash": "pw123",
        "device_id": "dev_004"
    }).json()
    user_id = reg["user_id"]

    # 查詢 user
    response = client.get(f"/v1/users/{user_id}")
    assert response.status_code == 200

    data = response.json()
    assert data["user_id"] == user_id
    assert data["email"] == "info@example.com"
    assert "created_at" in data

# ------------------------------------------------------
# 6. Test list trusted API (post)
# ------------------------------------------------------
def test_list_trusted_contacts():
    login = client.post("/v1/auth/login", json={
        "email": "contactlist@example.com",
        "password_hash": "pw999",
        "device_id": "dev_005"
    }).json()
    user_id = login["user_id"]

    # 先加一個聯絡人
    client.post(f"/v1/users/{user_id}/trusted-contacts", json={
        "name": "Bob",
        "phone": "+353800000222"
    })

    response = client.get(f"/v1/users/{user_id}/trusted-contacts")
    assert response.status_code == 200

    data = response.json()
    assert data["user_id"] == user_id
    assert isinstance(data["contacts"], list)
    assert len(data["contacts"]) >= 1
