# pytest services/user_management/tests/test_main.py -q
# pytest services/user_management/tests/test_main.py -k test_register_user -q

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.user_management.main import app, get_db


# ----------------------------
# Helpers: fake db + fake result
# ----------------------------
class FakeResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class FakeDB:
    def __init__(self, user_to_return):
        self.user_to_return = user_to_return
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt):
        # 你也可以在這裡檢查 stmt 裡的 where 條件，但通常 unit test 不需要
        return FakeResult(self.user_to_return)


def make_user(
    user_id: uuid.UUID,
    email="u@example.com",
    name="User",
    phone="+353000000000",
    created_at=None,
    updated_at=None,
    last_login=None,
):
    # 用 SimpleNamespace 模擬 ORM instance (user.user_id, user.email...)
    return SimpleNamespace(
        user_id=user_id,
        email=email,
        name=name,
        phone=phone,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=updated_at,  # 允許 None
        last_login=last_login,  # 允許 None
    )


@pytest.fixture()
def client():
    # 每個 test 都用乾淨 override
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------
# Tests
# ----------------------------
def test_get_user_success(client):
    uid = uuid.uuid4()
    fake_user = make_user(uid, email="test@example.com", name="Test", phone="+353123")

    fake_db = FakeDB(user_to_return=fake_user)

    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db

    res = client.get(f"/v1/users/{uid}")
    assert res.status_code == 200, res.text

    data = res.json()
    assert data["user_id"] == str(uid) or data["user_id"] == uid
    assert data["email"] == "test@example.com"
    assert data["name"] == "Test"
    assert data["phone"] == "+353123"
    assert "created_at" in data


def test_get_user_invalid_uuid_400(client):
    fake_db = FakeDB(user_to_return=None)

    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db

    res = client.get("/v1/users/not-a-uuid")
    assert res.status_code == 400
    assert res.json()["detail"] == "Invalid user_id format"


def test_get_user_not_found_404(client):
    uid = uuid.uuid4()
    fake_db = FakeDB(user_to_return=None)

    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db

    res = client.get(f"/v1/users/{uid}")
    assert res.status_code == 404
    assert res.json()["detail"] == f"User {uid} not found"


# # ------------------------------------------------------
# # 2. Test login API (post)
# # ------------------------------------------------------
# def test_login_user():
#     # Register user first to ensure user exists
#     reg = client.post(
#         "/v1/users/register",
#         json={
#             "email": "testuser@example.com",
#             "password_hash": "hash123",
#             "device_id": "dev_001",
#         },
#     )
#     assert reg.status_code == 200

#     # Now login with correct credentials
#     payload = {
#         "email": "testuser@example.com",
#         "password_hash": "hash123",
#         "device_id": "dev_001",
#     }

#     response = client.post("/v1/auth/login", json=payload)
#     assert response.status_code == 200

#     data = response.json()
#     assert data["status"] == "authenticated"
#     assert data["email"] == payload["email"]
#     assert "user_id" in data


# def test_login_success():
#     client.post(
#         "/v1/users/register",
#         json={
#             "email": "login@example.com",
#             "password_hash": "abc",
#             "device_id": "devx",
#         },
#     )

#     res = client.post(
#         "/v1/auth/login",
#         json={
#             "email": "login@example.com",
#             "password_hash": "abc",
#             "device_id": "devx",
#         },
#     )

#     assert res.status_code == 200
#     body = res.json()
#     assert body["status"] == "authenticated"
#     assert body["email"] == "login@example.com"


# # ------------------------------------------------------
# # 2b. Test login with wrong password
# # ------------------------------------------------------
# def test_login_wrong_password():
#     # Register user first
#     client.post(
#         "/v1/users/register",
#         json={
#             "email": "wrong@example.com",
#             "password_hash": "pw",
#             "device_id": "dev",
#         },
#     )

#     res = client.post(
#         "/v1/auth/login",
#         json={
#             "email": "wrong@example.com",
#             "password_hash": "badpw",
#             "device_id": "dev",
#         },
#     )

#     assert res.status_code == 401


# # ------------------------------------------------------
# # 2c. Test login with non-existent user
# # ------------------------------------------------------
# def test_login_nonexistent_user():
#     # Try to login with non-existent user
#     res = client.post(
#         "/v1/auth/login",
#         json={
#             "email": "ghost@example.com",
#             "password_hash": "x",
#             "device_id": "d",
#         },
#     )

#     assert res.status_code == 401


# # ------------------------------------------------------
# # 3. Test save preferences API (post)
# # ------------------------------------------------------
# def test_save_preferences():
#     # Register user first
#     reg = client.post(
#         "/v1/users/register",
#         json={
#             "email": "pref@example.com",
#             "password_hash": "hashx",
#             "device_id": "dev_002",
#         },
#     ).json()
#     user_id = reg["user_id"]

#     payload = {"voice_guidance": True, "safety_bias": "safest", "units": "metric"}

#     response = client.post(f"/v1/users/{user_id}/preferences", json=payload)
#     assert response.status_code == 200

#     data = response.json()
#     assert data["status"] == "preferences_saved"
#     assert data["preferences"]["voice_guidance"]
#     assert data["preferences"]["safety_bias"] == "safest"
#     assert "updated_at" in data


# # ------------------------------------------------------
# # 4. Test upsert trusted contact API (post)
# # ------------------------------------------------------
# def test_upsert_trusted_contact():
#     # Register user first
#     reg = client.post(
#         "/v1/users/register",
#         json={
#             "email": "contact@example.com",
#             "password_hash": "pw",
#             "device_id": "dev",
#         },
#     ).json()
#     uid = reg["user_id"]

#     # 新增
#     res = client.post(
#         f"/v1/users/{uid}/trusted-contacts",
#         json={"name": "Alice", "phone": "123"},
#     )
#     assert res.status_code == 200
#     contact_id = res.json()["contact_id"]

#     # 修改(existing)
#     res2 = client.post(
#         f"/v1/users/{uid}/trusted-contacts",
#         json={"contact_id": contact_id, "name": "Alice2", "phone": "123"},
#     )
#     assert res2.status_code == 200
#     assert res2.json()["contact"]["name"] == "Alice2"


# # ------------------------------------------------------
# # 5. Test get user API (get)
# # ------------------------------------------------------
# def test_get_user_info():
#     # Create user first
#     reg = client.post(
#         "/v1/users/register",
#         json={
#             "email": "info@example.com",
#             "password_hash": "pw123",
#             "device_id": "dev_004",
#         },
#     ).json()
#     user_id = reg["user_id"]

#     # Query user
#     response = client.get(f"/v1/users/{user_id}")
#     assert response.status_code == 200

#     data = response.json()
#     assert data["user_id"] == user_id
#     assert data["email"] == "info@example.com"
#     assert "created_at" in data


# # ------------------------------------------------------
# # 6. Test list trusted contacts API (get)
# # ------------------------------------------------------
# def test_list_trusted_contacts():
#     # Register user first
#     reg = client.post(
#         "/v1/users/register",
#         json={
#             "email": "list@example.com",
#             "password_hash": "pw",
#             "device_id": "dev",
#         },
#     ).json()

#     uid = reg["user_id"]

#     client.post(
#         f"/v1/users/{uid}/trusted-contacts",
#         json={"name": "A", "phone": "1"},
#     )
#     client.post(
#         f"/v1/users/{uid}/trusted-contacts",
#         json={"name": "B", "phone": "2"},
#     )

#     res = client.get(f"/v1/users/{uid}/trusted-contacts")
#     assert res.status_code == 200
#     assert len(res.json()["contacts"]) == 2
