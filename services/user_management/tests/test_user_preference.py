# pytest services/user_management/tests/test_user_preference.py -q

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from services.user_management.main import app, get_db


# ----------------------------
# Fake DB / Result
# ----------------------------
class FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class FakeDB:
    """
    Fake async SQLAlchemy session that:
      - returns queued execute results (execute_results)
      - records .add() calls
      - simulates flush/commit/rollback
    """

    def __init__(self, *, execute_results=None, commit_raises: Exception | None = None):
        self.execute_results = list(execute_results) if execute_results is not None else []
        self.commit_raises = commit_raises

        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False

        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt):
        if self.execute_results:
            return FakeResult(self.execute_results.pop(0))
        return FakeResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True
        now = datetime.now(timezone.utc)
        for obj in self.added:
            if hasattr(obj, "created_at") and getattr(obj, "created_at", None) is None:
                obj.created_at = now
            if hasattr(obj, "updated_at") and getattr(obj, "updated_at", None) is None:
                obj.updated_at = now

    async def commit(self):
        if self.commit_raises:
            raise self.commit_raises
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def override_db(fake_db: FakeDB):
    async def _override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture()
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------
# Helpers: fake ORM-like objects
# ----------------------------
def make_user(user_id: str):
    return SimpleNamespace(user_id=user_id)


def make_pref(
    user_id: str,
    *,
    voice_guidance: bool = True,
    units: str = "metric",
    updated_at=None,
):
    return SimpleNamespace(
        user_id=user_id,
        voice_guidance=voice_guidance,
        units=units,
        updated_at=updated_at or datetime.now(timezone.utc),
    )


# ----------------------------
# GET /preferences
# ----------------------------
def test_get_preferences_success(client):
    uid = "test-user-pref-001"

    fake_db = FakeDB(
        execute_results=[make_user(uid), make_pref(uid, voice_guidance=True, units="metric")]
    )
    override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/preferences")
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "preferences_loaded"
    assert data["user_id"] == uid
    assert data["preferences"]["voice_guidance"] is True
    assert data["preferences"]["units"] == "metric"
    assert "updated_at" in data


def test_get_preferences_user_not_found_404(client):
    uid = "nonexistent-user"

    fake_db = FakeDB(execute_results=[None])  # user lookup returns None
    override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/preferences")
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


def test_get_preferences_not_found_404(client):
    uid = "test-user-pref-002"

    fake_db = FakeDB(execute_results=[make_user(uid), None])  # pref lookup returns None
    override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/preferences")
    assert res.status_code == 404
    assert res.json()["detail"] == "Preferences not found"


# ----------------------------
# POST /preferences
# ----------------------------
def test_save_preferences_create_success(client):
    uid = "test-user-pref-003"

    # user exists, pref does not exist -> create
    fake_db = FakeDB(execute_results=[make_user(uid), None])
    override_db(fake_db)

    payload = {"voice_guidance": False, "units": "imperial"}

    res = client.post(f"/v1/users/{uid}/preferences", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "preferences_saved"
    assert data["user_id"] == uid
    assert data["preferences"]["voice_guidance"] is False
    assert data["preferences"]["units"] == "imperial"
    assert "updated_at" in data

    # create path: add(pref) + flush + add(audit) + commit
    assert fake_db.flushed is True
    assert fake_db.committed is True
    assert fake_db.rolled_back is False
    assert len(fake_db.added) == 2


def test_save_preferences_update_success(client):
    uid = "test-user-pref-004"

    existing_pref = make_pref(uid, voice_guidance=True, units="metric")
    fake_db = FakeDB(execute_results=[make_user(uid), existing_pref])
    override_db(fake_db)

    payload = {"voice_guidance": True, "units": "imperial"}

    res = client.post(f"/v1/users/{uid}/preferences", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "preferences_saved"
    assert data["preferences"]["voice_guidance"] is True
    assert data["preferences"]["units"] == "imperial"

    # update path: does NOT add(pref) and does NOT flush, only add(audit) + commit
    assert fake_db.flushed is False
    assert fake_db.committed is True
    assert len(fake_db.added) == 1


def test_save_preferences_user_not_found_404(client):
    uid = "test-user-pref-005"

    fake_db = FakeDB(execute_results=[None])  # user not found
    override_db(fake_db)

    payload = {"voice_guidance": True, "units": "metric"}
    res = client.post(f"/v1/users/{uid}/preferences", json=payload)

    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


def test_save_preferences_integrity_error_400(client):
    uid = "test-user-pref-006"

    fake_db = FakeDB(
        execute_results=[make_user(uid), None],
        commit_raises=IntegrityError("stmt", "params", Exception("orig")),
    )
    override_db(fake_db)

    payload = {"voice_guidance": True, "units": "metric"}
    res = client.post(f"/v1/users/{uid}/preferences", json=payload)

    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "Could not update preference"
    assert fake_db.rolled_back is True
