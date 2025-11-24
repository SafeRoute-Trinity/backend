<<<<<<< HEAD
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import libs.db as db
from models.user_models import Base
from services.user_management.main import app

DATABASE_URL = "sqlite+aiosqlite:///:memory:"

async_engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

AsyncTestingSessionLocal = sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ---- IMPORTANT PATCH ----
db.engine = async_engine
db.AsyncSessionLocal = AsyncTestingSessionLocal


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    import asyncio

    async def init_models():
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_models():
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    # ---- RUN create_all() ----
    asyncio.get_event_loop().run_until_complete(init_models())

    yield

    # ---- RUN drop_all() ----
    asyncio.get_event_loop().run_until_complete(drop_models())


# override dependency
async def override_get_db():
    async with AsyncTestingSessionLocal() as session:
        yield session


app.dependency_overrides[db.get_db] = override_get_db
client = TestClient(app)


# -------------------------
# 3. Mock Redis
# -------------------------
class MockRedis:
    def __init__(self):
        self.store = {}

    def is_connected(self):
        return True

    def set(self, key, value, ttl=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def set_json(self, key, value, ttl=None):
        self.store[key] = value

    def get_json(self, key):
        return self.store.get(key)


# 注入 Fake Redis
import services.user_management.main as UM

UM.redis_client = MockRedis()


# =====================================================
# 🔥                Begin Tests
# =====================================================


def test_register_user():
    res = client.post(
        "/v1/users/register",
        json={
            "email": "test@example.com",
            "password_hash": "pw123",
            "device_id": "dev1",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "created"
    assert body["email"] == "test@example.com"
    assert "user_id" in body


def test_register_duplicate_email():
    client.post(
        "/v1/users/register",
        json={
            "email": "dup@example.com",
            "password_hash": "pw1",
            "device_id": "d1",
        },
    )

    res = client.post(
        "/v1/users/register",
        json={
            "email": "dup@example.com",
            "password_hash": "pw2",
            "device_id": "d2",
        },
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "Email already registered"


def test_login_success():
    client.post(
        "/v1/users/register",
        json={
            "email": "login@example.com",
            "password_hash": "abc",
            "device_id": "devx",
        },
    )

    res = client.post(
        "/v1/auth/login",
        json={
            "email": "login@example.com",
            "password_hash": "abc",
            "device_id": "devx",
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "authenticated"
    assert body["email"] == "login@example.com"


def test_login_wrong_password():
    client.post(
        "/v1/users/register",
        json={
            "email": "wrong@example.com",
            "password_hash": "pw",
            "device_id": "dev",
        },
    )

    res = client.post(
        "/v1/auth/login",
        json={
            "email": "wrong@example.com",
            "password_hash": "badpw",
            "device_id": "dev",
        },
    )

    assert res.status_code == 401


def test_login_nonexistent_user():
    res = client.post(
        "/v1/auth/login",
        json={
            "email": "ghost@example.com",
            "password_hash": "x",
            "device_id": "d",
        },
    )

    assert res.status_code == 401


def test_save_preferences():
=======
# pytest services/user_management/tests/test_main.py -q
# pytest services/user_management/tests/test_main.py -k test_register_user -q


import re

from fastapi.testclient import TestClient

from services.user_management.main import app

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
        "name": "Test User",
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
    # Register user first to ensure user exists
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "testuser@example.com",
            "password_hash": "hash123",
            "device_id": "dev_001",
        },
    )
    assert reg.status_code == 200

    # Now login with correct credentials
    payload = {
        "email": "testuser@example.com",
        "password_hash": "hash123",
        "device_id": "dev_001",
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
# 2b. Test login with wrong password
# ------------------------------------------------------
def test_login_wrong_password():
    # Register user first
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "wrongpass@example.com",
            "password_hash": "correct_hash",
            "device_id": "dev_001",
        },
    )
    assert reg.status_code == 200

    # Try to login with wrong password
    response = client.post(
        "/v1/auth/login",
        json={
            "email": "wrongpass@example.com",
            "password_hash": "wrong_hash",
            "device_id": "dev_001",
        },
    )
    assert response.status_code == 401
    assert "Invalid email or password" in response.json()["detail"]


# ------------------------------------------------------
# 2c. Test login with non-existent user
# ------------------------------------------------------
def test_login_nonexistent_user():
    # Try to login with non-existent user
    response = client.post(
        "/v1/auth/login",
        json={
            "email": "nonexistent@example.com",
            "password_hash": "any_hash",
            "device_id": "dev_001",
        },
    )
    assert response.status_code == 401
    assert "Invalid email or password" in response.json()["detail"]


# ------------------------------------------------------
# 3. Test save preferences API (post)
# ------------------------------------------------------
def test_save_preferences():
    # Register user first
>>>>>>> 27347c6 (chore: OpenRouterService Integration)
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "pref@example.com",
<<<<<<< HEAD
            "password_hash": "pw",
            "device_id": "dev",
        },
    ).json()

    uid = reg["user_id"]

    res = client.post(
        f"/v1/users/{uid}/preferences",
        json={"voice_guidance": "on", "safety_bias": "safest"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "preferences_saved"


def test_upsert_trusted_contact():
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "contact@example.com",
            "password_hash": "pw",
            "device_id": "dev",
        },
    ).json()

    uid = reg["user_id"]

    # 新增
    res = client.post(
        f"/v1/users/{uid}/trusted-contacts",
        json={"name": "Alice", "phone": "123"},
    )
    assert res.status_code == 200
    contact_id = res.json()["contact_id"]

    # 修改(existing)
    res2 = client.post(
        f"/v1/users/{uid}/trusted-contacts",
        json={"contact_id": contact_id, "name": "Alice2", "phone": "123"},
    )
    assert res2.status_code == 200
    assert res2.json()["contact"]["name"] == "Alice2"


def test_list_trusted_contacts():
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "list@example.com",
            "password_hash": "pw",
            "device_id": "dev",
        },
    ).json()

    uid = reg["user_id"]

    client.post(
        f"/v1/users/{uid}/trusted-contacts",
        json={"name": "A", "phone": "1"},
    )
    client.post(
        f"/v1/users/{uid}/trusted-contacts",
        json={"name": "B", "phone": "2"},
    )

    res = client.get(f"/v1/users/{uid}/trusted-contacts")
    assert res.status_code == 200
    assert len(res.json()["contacts"]) == 2
=======
            "password_hash": "hashx",
            "device_id": "dev_002",
        },
    ).json()
    user_id = reg["user_id"]

    payload = {"voice_guidance": "on", "safety_bias": "safest", "units": "metric"}

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
    # Register user first
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "trusted@example.com",
            "password_hash": "hashz",
            "device_id": "dev_003",
        },
    ).json()
    user_id = reg["user_id"]

    payload = {
        "name": "Alice",
        "phone": "+353800000111",
        "relationship": "friend",
        "is_primary": True,
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
    # Create user first
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "info@example.com",
            "password_hash": "pw123",
            "device_id": "dev_004",
        },
    ).json()
    user_id = reg["user_id"]

    # Query user
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
    # Register user first
    reg = client.post(
        "/v1/users/register",
        json={
            "email": "contactlist@example.com",
            "password_hash": "pw999",
            "device_id": "dev_005",
        },
    ).json()
    user_id = reg["user_id"]

    # Add a contact first
    client.post(
        f"/v1/users/{user_id}/trusted-contacts",
        json={"name": "Bob", "phone": "+353800000222"},
    )

    response = client.get(f"/v1/users/{user_id}/trusted-contacts")
    assert response.status_code == 200

    data = response.json()
    assert data["user_id"] == user_id
    assert isinstance(data["contacts"], list)
    assert len(data["contacts"]) >= 1
>>>>>>> 27347c6 (chore: OpenRouterService Integration)
