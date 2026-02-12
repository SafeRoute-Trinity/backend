# pytest services/user_management/tests/test_main.py -q
# pytest services/user_management/tests/test_main.py -k test_get_user_success -q

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import services.user_management.main as um
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
    def __init__(
        self,
        user_to_return=None,
        *,
        commit_raises: Exception | None = None,
    ):
        # 用於 GET / duplicate check
        self.user_to_return = user_to_return

        # 用於 register 流程驗證
        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.commit_raises = commit_raises

        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt):
        return FakeResult(self.user_to_return)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

        # 模擬 DB defaults：把 User.created_at/updated_at 補起來
        now = datetime.now(timezone.utc)

        for obj in self.added:
            # 用 duck-typing 判斷是 User 物件：有 email/password 這些欄位
            if hasattr(obj, "email") and hasattr(obj, "password"):
                if getattr(obj, "created_at", None) is None:
                    obj.created_at = now
                if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
                    obj.updated_at = now

    async def commit(self):
        if self.commit_raises:
            raise self.commit_raises
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


# ----------------------------
# Fake ORM models used by register_user
# ----------------------------
class FakeUser:
    def __init__(self, user_id, email, password, phone, name, last_login=None):
        self.user_id = user_id
        self.email = email
        self.password = password
        self.phone = phone
        self.name = name
        self.last_login = last_login
        # register_user uses getattr(user, "created_at", now)
        self.created_at = None


class FakeAudit:
    def __init__(self, log_id, user_id, event_type, event_id, message, created_at, updated_at):
        self.log_id = log_id
        self.user_id = user_id
        self.event_type = event_type
        self.event_id = event_id
        self.message = message
        self.created_at = created_at
        self.updated_at = updated_at


def _override_db(fake_db: FakeDB):
    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db


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


# ----------------------------
# Register tests (mock, no SQLite)
# ----------------------------


def test_register_user_success(client, monkeypatch):
    # ✅ 不要 patch um.User / um.Audit（讓 select(User) 正常）
    # ✅ 只 patch in-memory store + metric + TTL

    monkeypatch.setattr(um, "users", {}, raising=False)

    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(um, "USER_REGISTRATION_TOTAL", counter, raising=False)
    monkeypatch.setattr(um, "AUTH_TOKEN_TTL", 3600, raising=False)

    fake_db = FakeDB(user_to_return=None)
    _override_db(fake_db)

    payload = {
        "email": "testuser@example.com",
        "password": "plain123",
        "phone": "+353123456789",
        "name": "Test User",
    }

    res = client.post("/v1/users/register", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "created"
    assert data["email"] == payload["email"]
    assert data["phone"] == payload["phone"]
    assert data["name"] == payload["name"]
    uuid.UUID(data["user_id"])

    assert data["auth"]["token"].startswith("atk_")
    assert data["auth"]["expires_in"] == 3600

    # DB calls captured
    assert fake_db.flushed is True
    assert fake_db.committed is True
    assert fake_db.rolled_back is False
    assert len(fake_db.added) == 2  # User + Audit (真 ORM 物件，但我們只記錄，不會進 DB)

    # in-memory users updated
    created_uid = uuid.UUID(data["user_id"])
    assert created_uid in um.users
    assert um.users[created_uid]["email"] == payload["email"]

    # metric incremented once
    assert um.USER_REGISTRATION_TOTAL.count == 1


def test_register_user_duplicate_email_400(client, monkeypatch):
    monkeypatch.setattr(um, "users", {}, raising=False)

    # simulate "email already exists"
    fake_db = FakeDB(user_to_return=SimpleNamespace(email="testuser@example.com"))
    _override_db(fake_db)

    payload = {
        "email": "testuser@example.com",
        "password": "plain123",
        "phone": "+353123456789",
        "name": "Test User",
    }

    res = client.post("/v1/users/register", json=payload)
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "Email already registered"

    assert fake_db.added == []
    assert fake_db.flushed is False
    assert fake_db.committed is False


def test_register_user_integrity_error_400(client, monkeypatch):
    monkeypatch.setattr(um, "users", {}, raising=False)

    fake_db = FakeDB(
        user_to_return=None,
        commit_raises=IntegrityError("stmt", "params", Exception("orig")),
    )
    _override_db(fake_db)

    payload = {
        "email": "unique@example.com",
        "password": "plain123",
        "phone": "+353999999999",
        "name": "New User",
    }

    res = client.post("/v1/users/register", json=payload)
    assert res.status_code == 400, res.text

    detail = res.json()["detail"]
    assert detail.startswith("Could not create user")  # allow extra error info

    assert fake_db.rolled_back is True
    assert fake_db.committed is False
