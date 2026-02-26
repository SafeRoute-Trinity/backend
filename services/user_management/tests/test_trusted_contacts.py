# pytest services/user_management/tests/test_trusted_contacts.py -q
# pytest services/user_management/tests/test_trusted_contacts.py -k test_list_trusted_contacts_success -q


import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from services.user_management.main import app, get_db


# ----------------------------
# Fake DB helpers
# ----------------------------
class FakeScalarsResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class FakeExecuteResult:
    """Mock result for db.execute(); supports scalar_one() and scalars().all()."""

    def __init__(self, scalar_one_value=None, scalars_all=None):
        self._scalar_one_value = scalar_one_value
        self._scalars_all = list(scalars_all) if scalars_all is not None else []

    def scalar_one(self):
        return self._scalar_one_value

    def scalars(self):
        return FakeScalarsResult(self._scalars_all)


class FakeDB:
    """
    Mock async db session for endpoints that use:
      - await db.scalar(...)
      - await db.scalars(...).all()
      - db.add(...)
      - await db.flush()
      - await db.commit() / await db.rollback()
    """

    def __init__(
        self,
        *,
        scalar_results=None,  # queue for db.scalar(...)
        scalars_results=None,  # queue for db.scalars(...)
        execute_results=None,  # queue for db.execute(...) (FakeExecuteResult)
        commit_raises: Exception | None = None,
    ):
        self.scalar_results = list(scalar_results) if scalar_results is not None else []
        self.scalars_results = list(scalars_results) if scalars_results is not None else []
        self.execute_results = list(execute_results) if execute_results is not None else []
        self.commit_raises = commit_raises

        self.added = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False

    async def scalar(self, stmt):
        return self.scalar_results.pop(0) if self.scalar_results else None

    async def scalars(self, stmt):
        items = self.scalars_results.pop(0) if self.scalars_results else []
        return FakeScalarsResult(items)

    async def execute(self, stmt):
        return self.execute_results.pop(0) if self.execute_results else FakeExecuteResult()

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
# Fake ORM-like objects
# ----------------------------
def make_user(user_id: str):
    return SimpleNamespace(user_id=user_id)


def make_contact(
    *,
    contact_id: uuid.UUID,
    user_id: str,
    name: str,
    phone: str,
    relation: str,
    is_primary: bool = False,
    created_at=None,
    updated_at=None,
):
    return SimpleNamespace(
        contact_id=contact_id,
        user_id=user_id,
        name=name,
        phone=phone,
        relation=relation,
        is_primary=is_primary,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=updated_at or datetime.now(timezone.utc),
    )


# ----------------------------
# GET /trusted-contacts
# ----------------------------
def test_list_trusted_contacts_success(client):
    uid = "test-user-contacts-001"
    c1 = make_contact(
        contact_id=uuid.uuid4(),
        user_id=uid,
        name="Alice",
        phone="+353111111111",
        relation="friend",
        is_primary=True,
    )
    c2 = make_contact(
        contact_id=uuid.uuid4(),
        user_id=uid,
        name="Bob",
        phone="+353222222222",
        relation="family",
        is_primary=False,
    )

    fake_db = FakeDB(
        scalar_results=[make_user(uid)],  # user exists
        execute_results=[
            FakeExecuteResult(scalar_one_value=2),  # count for pagination
        ],
        scalars_results=[[c1, c2]],  # contacts list (paginated query)
    )
    override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/trusted-contacts")
    assert res.status_code == 200, res.text
    data = res.json()

    assert str(uid) == str(data["user_id"])
    assert isinstance(data["data"], list)
    assert len(data["data"]) == 2
    assert data["data"][0]["phone"] == "+353111111111"
    assert data["data"][0]["is_primary"] is True
    assert data["data"][1]["phone"] == "+353222222222"
    assert "filters" in data
    assert data["filters"]["is_primary"] == ""
    assert data["filters"]["search"] == ""
    assert "pagination" in data
    assert data["pagination"]["page"] == 1
    assert data["pagination"]["page_size"] == 20
    assert data["pagination"]["total"] == 2
    assert data["pagination"]["total_pages"] == 1


def test_list_trusted_contacts_user_not_found_404(client):
    uid = "nonexistent-user"
    fake_db = FakeDB(scalar_results=[None])  # user not found
    override_db(fake_db)

    res = client.get(f"/v1/users/{uid}/trusted-contacts")
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


# ----------------------------
# POST /trusted-contacts (upsert)
# ----------------------------
def test_upsert_trusted_contact_create_success(client):
    uid = "test-user-contacts-002"

    # scalar() calls order inside endpoint:
    # (1) user exists -> user
    # (2) contact lookup -> None  => create
    fake_db = FakeDB(scalar_results=[make_user(uid), None])
    override_db(fake_db)

    payload = {
        "name": "Alice",
        "phone": "+353111111111",
        "relationship": "friend",
        "is_primary": True,
    }

    res = client.post(f"/v1/users/{uid}/trusted-contacts", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "contact_upserted"
    assert data["user_id"] == uid
    assert data["contact"]["phone"] == "+353111111111"
    assert data["contact"]["name"] == "Alice"
    assert data["contact"]["relation"] == "friend"
    assert data["contact"]["is_primary"] is True
    assert "updated_at" in data

    # create path: db.add(contact) + flush + add(audit) + commit
    assert fake_db.flushed is True
    assert fake_db.committed is True
    assert fake_db.rolled_back is False
    assert len(fake_db.added) == 2


def test_upsert_trusted_contact_update_success(client):
    uid = "test-user-contacts-003"
    existing = make_contact(
        contact_id=uuid.uuid4(),
        user_id=uid,
        name="OldName",
        phone="+353111111111",
        relation="friend",
        is_primary=False,
    )

    # scalar() order:
    # (1) user exists
    # (2) contact exists -> update
    fake_db = FakeDB(scalar_results=[make_user(uid), existing])
    override_db(fake_db)

    payload = {
        "name": "NewName",
        "phone": "+353111111111",
        "relationship": "family",
        "is_primary": True,
    }

    res = client.post(f"/v1/users/{uid}/trusted-contacts", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "contact_upserted"
    assert data["contact"]["name"] == "NewName"
    assert data["contact"]["relation"] == "family"
    assert data["contact"]["is_primary"] is True

    # update path: no db.add(contact), no flush, add(audit) + commit
    assert fake_db.flushed is False
    assert fake_db.committed is True
    assert len(fake_db.added) == 1  # only audit


def test_upsert_trusted_contact_user_not_found_404(client):
    uid = "nonexistent-user"
    fake_db = FakeDB(scalar_results=[None])  # user not found
    override_db(fake_db)

    payload = {
        "name": "Alice",
        "phone": "+353111111111",
        "relationship": "friend",
        "is_primary": True,
    }

    res = client.post(f"/v1/users/{uid}/trusted-contacts", json=payload)
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


def test_upsert_trusted_contact_integrity_error_400(client):
    uid = "test-user-contacts-004"

    fake_db = FakeDB(
        scalar_results=[make_user(uid), None],  # user exists, contact not exists -> create
        commit_raises=IntegrityError("stmt", "params", Exception("orig")),
    )
    override_db(fake_db)

    payload = {
        "name": "Alice",
        "phone": "+353111111111",
        "relationship": "friend",
        "is_primary": True,
    }

    res = client.post(f"/v1/users/{uid}/trusted-contacts", json=payload)
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "Could not upsert trusted contact"
    assert fake_db.rolled_back is True
