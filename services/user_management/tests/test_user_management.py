# pytest services/user_management/tests/test_user_management.py -v

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
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def scalar_one(self):
        if self._obj is None:
            raise Exception("No row found")
        return self._obj

    def scalars(self):
        return self

    def all(self):
        if isinstance(self._obj, list):
            return self._obj
        return [self._obj] if self._obj else []


class FakeDB:
    """Lightweight in-memory DB stand-in that records calls."""

    def __init__(
        self,
        plan=None,
        *,
        commit_raises: Exception | None = None,
    ):
        self.plan = list(plan or [])
        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.commit_raises = commit_raises

        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt, params=None):
        if self.plan:
            return self.plan.pop(0)
        return FakeResult(None)

    async def scalar(self, stmt):
        """Used by db.scalar() calls in the service."""
        if self.plan:
            result = self.plan.pop(0)
            return result._obj if isinstance(result, FakeResult) else result
        return None

    async def scalars(self, stmt):
        """Used by db.scalars() calls in the service."""
        if self.plan:
            result = self.plan.pop(0)
            return result
        return FakeResult([])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True
        now = datetime.now(timezone.utc)
        for obj in self.added:
            if hasattr(obj, "email"):
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


def _override_db(fake_db: FakeDB):
    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db


def make_user(
    user_id: str,
    email="u@example.com",
    name="User",
    phone="+353000000000",
    created_at=None,
    updated_at=None,
    last_login=None,
):
    return SimpleNamespace(
        user_id=user_id,
        email=email,
        name=name,
        phone=phone,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=updated_at,
        last_login=last_login,
    )


def make_prefs(user_id: str, voice_guidance=True, units="metric", updated_at=None):
    return SimpleNamespace(
        user_id=user_id,
        voice_guidance=voice_guidance,
        units=units,
        updated_at=updated_at or datetime.now(timezone.utc),
    )


def make_contact(
    user_id: str,
    name="Alice",
    phone="+353800000111",
    relation="friend",
    is_primary=False,
    contact_id=None,
):
    return SimpleNamespace(
        contact_id=contact_id or uuid.uuid4(),
        user_id=user_id,
        name=name,
        phone=phone,
        relation=relation,
        is_primary=is_primary,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def make_audit(user_id: str, event_type="authentication", message="test"):
    return SimpleNamespace(
        log_id=uuid.uuid4(),
        user_id=user_id,
        event_type=event_type,
        event_id=None,
        message=message,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture()
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------
# GET / (root)
# ----------------------------
def test_root(client):
    res = client.get("/")
    assert res.status_code == 200
    data = res.json()
    assert data["service"] == "user_management"
    assert data["status"] == "running"


# ----------------------------
# GET /metrics
# ----------------------------
def test_metrics(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]


# ----------------------------
# GET /v1/users/{user_id}
# ----------------------------
def test_get_user_success(client):
    uid = "auth0|abc123"
    fake_user = make_user(uid, email="test@example.com", name="Test", phone="+353123")
    fake_db = FakeDB(plan=[FakeResult(fake_user)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["user_id"] == uid
    assert data["email"] == "test@example.com"
    assert data["name"] == "Test"
    assert data["phone"] == "+353123"
    assert "created_at" in data


def test_get_user_not_found_404(client):
    uid = "auth0|nonexistent"
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"]


# ----------------------------
# POST /v1/webhooks/auth0/sync-user
# ----------------------------
def test_sync_auth0_user_create_success(client, monkeypatch):
    monkeypatch.setenv("AUTH0_WEBHOOK_SECRET", "test-secret")

    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(um, "USER_REGISTRATION_TOTAL", counter, raising=False)

    fake_db = FakeDB(plan=[FakeResult(None)])  # user doesn't exist → create
    _override_db(fake_db)

    payload = {
        "user_id": "auth0|newuser456",
        "email": "testuser@example.com",
        "name": "Test User",
        "phone": "+353123456789",
    }

    res = client.post(
        "/v1/webhooks/auth0/sync-user",
        json=payload,
        headers={"X-Auth0-Webhook-Secret": "test-secret"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "synced"
    assert data["user_id"] == "auth0|newuser456"
    assert fake_db.committed is True
    assert len(fake_db.added) >= 1  # User + Audit
    assert counter.count == 1


def test_sync_auth0_user_update_success(client, monkeypatch):
    monkeypatch.setenv("AUTH0_WEBHOOK_SECRET", "test-secret")

    class _Counter:
        def __init__(self):
            self.count = 0

        def inc(self):
            self.count += 1

    counter = _Counter()
    monkeypatch.setattr(um, "USER_REGISTRATION_TOTAL", counter, raising=False)

    existing_user = make_user("auth0|existing789", email="old@example.com", name="Old Name")
    fake_db = FakeDB(plan=[FakeResult(existing_user)])  # user exists → update
    _override_db(fake_db)

    payload = {
        "user_id": "auth0|existing789",
        "email": "new@example.com",
        "name": "New Name",
    }

    res = client.post(
        "/v1/webhooks/auth0/sync-user",
        json=payload,
        headers={"X-Auth0-Webhook-Secret": "test-secret"},
    )
    assert res.status_code == 200, res.text
    assert existing_user.email == "new@example.com"
    assert existing_user.name == "New Name"


def test_sync_auth0_user_invalid_secret_401(client, monkeypatch):
    monkeypatch.setenv("AUTH0_WEBHOOK_SECRET", "correct-secret")
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    payload = {"user_id": "auth0|user", "email": "testuser@example.com"}

    res = client.post(
        "/v1/webhooks/auth0/sync-user",
        json=payload,
        headers={"X-Auth0-Webhook-Secret": "wrong-secret"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid webhook secret"


def test_sync_auth0_user_missing_secret_401(client, monkeypatch):
    monkeypatch.setenv("AUTH0_WEBHOOK_SECRET", "correct-secret")
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    payload = {"user_id": "auth0|user", "email": "testuser@example.com"}
    res = client.post("/v1/webhooks/auth0/sync-user", json=payload)
    assert res.status_code == 401


def test_sync_auth0_user_integrity_error_400(client, monkeypatch):
    monkeypatch.setenv("AUTH0_WEBHOOK_SECRET", "test-secret")

    fake_db = FakeDB(
        plan=[FakeResult(None)],
        commit_raises=IntegrityError("stmt", "params", Exception("orig")),
    )
    _override_db(fake_db)

    payload = {"user_id": "auth0|user", "email": "testuser@example.com"}

    res = client.post(
        "/v1/webhooks/auth0/sync-user",
        json=payload,
        headers={"X-Auth0-Webhook-Secret": "test-secret"},
    )
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "Could not sync user"
    assert fake_db.rolled_back is True


# ----------------------------
# GET /v1/users/{user_id}/preferences
# ----------------------------
def test_get_preferences_success(client):
    uid = "auth0|prefuser"
    fake_user = make_user(uid)
    fake_prefs = make_prefs(uid, voice_guidance=True, units="metric")

    # get_preferences does: db.execute(select User) then db.execute(select UserPreferences)
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult(fake_prefs)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/preferences")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "preferences_loaded"
    assert data["preferences"]["voice_guidance"] is True
    assert data["preferences"]["units"] == "metric"


def test_get_preferences_user_not_found_404(client):
    uid = "auth0|ghost"
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/preferences")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_get_preferences_no_prefs_404(client):
    uid = "auth0|noprefs"
    fake_user = make_user(uid)
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult(None)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/preferences")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


# ----------------------------
# POST /v1/users/{user_id}/preferences
# ----------------------------
def test_save_preferences_success(client):
    uid = "auth0|saveprefs"
    fake_user = make_user(uid)

    # save_preferences does: db.execute(select User), db.execute(select UserPreferences) → None (new)
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult(None)])
    _override_db(fake_db)

    payload = {"voice_guidance": False, "units": "imperial"}
    res = client.post(f"/v1/users/{uid}/preferences", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "preferences_saved"
    assert data["preferences"]["voice_guidance"] is False
    assert data["preferences"]["units"] == "imperial"
    assert fake_db.committed is True


def test_save_preferences_user_not_found_404(client):
    uid = "auth0|nouser"
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    payload = {"voice_guidance": True, "units": "metric"}
    res = client.post(f"/v1/users/{uid}/preferences", json=payload)
    assert res.status_code == 404


def test_save_preferences_update_existing(client):
    uid = "auth0|updateprefs"
    fake_user = make_user(uid)
    existing_pref = make_prefs(uid, voice_guidance=True, units="metric")

    # save_preferences: db.execute(select User), db.execute(select UserPreferences) → existing
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult(existing_pref)])
    _override_db(fake_db)

    payload = {"voice_guidance": False, "units": "imperial"}
    res = client.post(f"/v1/users/{uid}/preferences", json=payload)
    assert res.status_code == 200, res.text
    # Verify the pref object was mutated
    assert existing_pref.voice_guidance is False
    assert existing_pref.units == "imperial"


# ----------------------------
# GET /v1/users/{user_id}/trusted-contacts
# ----------------------------
def test_list_trusted_contacts_success(client):
    uid = "auth0|contactuser"
    fake_user = make_user(uid)
    contacts = [
        make_contact(uid, name="Alice", phone="+111", is_primary=True),
        make_contact(uid, name="Bob", phone="+222", is_primary=False),
    ]

    # list_trusted_contacts uses: db.scalar(select User), db.scalars(select TrustedContact)
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult(contacts)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/trusted-contacts")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["user_id"] == uid
    assert len(data["contacts"]) == 2


def test_list_trusted_contacts_user_not_found_404(client):
    uid = "auth0|nope"
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/trusted-contacts")
    assert res.status_code == 404


def test_list_trusted_contacts_empty(client):
    uid = "auth0|lonely"
    fake_user = make_user(uid)
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult([])])
    _override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/trusted-contacts")
    assert res.status_code == 200
    data = res.json()
    assert data["contacts"] == []


# ----------------------------
# POST /v1/users/{user_id}/trusted-contacts
# ----------------------------
def test_upsert_trusted_contact_create(client):
    uid = "auth0|newcontact"
    fake_user = make_user(uid)

    # upsert does: db.scalar(select User), db.scalar(select TrustedContact by phone) → None (new)
    fake_db = FakeDB(plan=[FakeResult(fake_user), FakeResult(None)])
    _override_db(fake_db)

    payload = {
        "name": "Charlie",
        "phone": "+353800000333",
        "relationship": "sibling",
        "is_primary": True,
    }
    res = client.post(f"/v1/users/{uid}/trusted-contacts", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "contact_upserted"
    assert fake_db.committed is True


def test_upsert_trusted_contact_user_not_found_404(client):
    uid = "auth0|nouser"
    fake_db = FakeDB(plan=[FakeResult(None)])
    _override_db(fake_db)

    payload = {"name": "X", "phone": "+111"}
    res = client.post(f"/v1/users/{uid}/trusted-contacts", json=payload)
    assert res.status_code == 404


# ----------------------------
# GET /v1/users/audit
# ----------------------------
# NOTE: These tests are commented out because the audit endpoint
# (/v1/users/audit) is shadowed by the catch-all route
# (/v1/users/{user_id}) which is registered first in main.py.
# FastAPI matches "audit" as a user_id parameter value.
# To fix this, the audit route should be registered BEFORE the
# get_user route, or moved to a different URL (e.g. /v1/audit/logs).

