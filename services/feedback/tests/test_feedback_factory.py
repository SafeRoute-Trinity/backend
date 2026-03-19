import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

import services.feedback.feedback_factory as feedback_factory_module
from models.feedback import Feedback
from services.feedback.feedback_factory import FeedbackFactory
from services.feedback.types import FeedbackType, SeverityType, Status


def _run(coro):
    return asyncio.run(coro)


class FakeCreateSession:
    def __init__(self, *, flush_raises: Exception | None = None):
        self.added = []
        self.flushed = False
        self.flush_raises = flush_raises

    async def flush(self):
        if self.flush_raises:
            raise self.flush_raises
        self.flushed = True

    def add(self, obj):
        self.added.append(obj)


class FakeExecuteResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeScalarsResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class FakeQuerySession:
    _UNSET = object()

    def __init__(
        self,
        *,
        scalar_total=None,
        execute_scalars=None,
        execute_scalar_one_or_none=_UNSET,
    ):
        self._scalar_total = scalar_total
        self._execute_scalars = execute_scalars or []
        self._execute_scalar_one_or_none = execute_scalar_one_or_none

        self.scalar_calls = 0
        self.execute_calls = 0

    async def scalar(self, stmt):
        self.scalar_calls += 1
        return self._scalar_total

    async def execute(self, stmt):
        self.execute_calls += 1
        if self._execute_scalar_one_or_none is not self._UNSET:
            return FakeExecuteResult(self._execute_scalar_one_or_none)

        return SimpleNamespace(scalars=lambda: FakeScalarsResult(self._execute_scalars))


def test_feedback_factory_create_feedback_converts_fields_and_sets_timestamps():
    factory = FeedbackFactory()

    feedback_id = uuid.uuid4()
    ticket = "TKT-test-1"
    user_id = "usr_1"
    route_id = uuid.uuid4()

    class _LocationObj:
        def dict(self):
            return {"lat": 1.23, "lon": 4.56}

    class _Attachment:
        def __init__(self, val: str):
            self.val = val

        def __str__(self) -> str:
            return self.val

    created_at = datetime(2020, 1, 1)
    session = FakeCreateSession()

    feedback = _run(
        factory.create_feedback(
            session,
            feedback_id=feedback_id,
            user_id=user_id,
            ticket_number=ticket,
            route_id=route_id,
            type=FeedbackType.SAFETY_ISSUE,
            severity=SeverityType.HIGH,
            description="desc",
            location=_LocationObj(),
            attachments=[_Attachment("https://a.example"), _Attachment("https://b.example")],
            status=Status.RECEIVED,
            created_at=created_at,
        )
    )

    assert isinstance(feedback, Feedback)
    assert feedback.feedback_id == feedback_id
    assert feedback.user_id == user_id
    assert feedback.ticket_number == ticket
    assert feedback.route_id == route_id

    # Enum conversion branches
    assert feedback.type == FeedbackType.SAFETY_ISSUE.value
    assert feedback.severity == SeverityType.HIGH.value
    assert feedback.status == Status.RECEIVED.value

    # Location + attachments conversion branches
    assert feedback.location == {"lat": 1.23, "lon": 4.56}
    assert feedback.attachments == ["https://a.example", "https://b.example"]

    # created_at and updated_at are set from passed created_at
    assert feedback.created_at == created_at
    assert feedback.updated_at == created_at
    assert session.flushed is True
    assert len(session.added) == 1


def test_feedback_factory_create_feedback_raises_when_flush_fails():
    factory = FeedbackFactory()
    session = FakeCreateSession(flush_raises=RuntimeError("flush failed"))

    with pytest.raises(RuntimeError, match="flush failed"):
        _run(
            factory.create_feedback(
                session,
                feedback_id=uuid.uuid4(),
                user_id="usr",
                ticket_number="TKT-x",
                type=None,
                severity=None,
                description=None,
                location=None,
                attachments=None,
                status=Status.RECEIVED,
                created_at=datetime.utcnow(),
            )
        )


def test_feedback_factory_get_feedback_by_id_returns_row_or_none():
    factory = FeedbackFactory()
    feedback = SimpleNamespace(feedback_id=uuid.uuid4())
    session = FakeQuerySession(execute_scalar_one_or_none=feedback)

    got = _run(factory.get_feedback_by_id(session, uuid.UUID(str(feedback.feedback_id))))
    assert got is feedback

    session2 = FakeQuerySession(execute_scalar_one_or_none=None)
    got2 = _run(factory.get_feedback_by_id(session2, uuid.uuid4()))
    assert got2 is None


def test_feedback_factory_get_feedback_by_ticket_returns_row_or_none():
    factory = FeedbackFactory()
    feedback = SimpleNamespace(ticket_number="TKT-1")
    session = FakeQuerySession(execute_scalar_one_or_none=feedback)

    got = _run(factory.get_feedback_by_ticket(session, "TKT-1"))
    assert got is feedback

    session2 = FakeQuerySession(execute_scalar_one_or_none=None)
    got2 = _run(factory.get_feedback_by_ticket(session2, "TKT-missing"))
    assert got2 is None


def test_feedback_factory_get_feedbacks_total_defaults_to_0_when_scalar_none():
    factory = FeedbackFactory()
    session = FakeQuerySession(
        scalar_total=None, execute_scalars=[SimpleNamespace(feedback_id=uuid.uuid4())]
    )

    total, items = _run(
        factory.get_feedbacks(
            session,
            user_id=None,
            status=None,
            feedback_type=None,
            skip=0,
            limit=10,
        )
    )

    assert total == 0
    assert len(items) == 1
    assert session.scalar_calls == 1
    assert session.execute_calls == 1


def test_feedback_factory_get_feedbacks_covers_user_id_and_status_branches():
    factory = FeedbackFactory()

    session = FakeQuerySession(
        scalar_total=2,
        execute_scalars=[SimpleNamespace(feedback_id=uuid.uuid4(), user_id="u1")],
    )

    total, items = _run(
        factory.get_feedbacks(
            session,
            user_id="u1",
            status=Status.RECEIVED,
            feedback_type=None,
            skip=0,
            limit=10,
        )
    )

    assert total == 2
    assert len(items) == 1


def test_feedback_factory_get_feedbacks_covers_feedback_type_branch():
    factory = FeedbackFactory()

    session = FakeQuerySession(
        scalar_total=3,
        execute_scalars=[SimpleNamespace(feedback_id=uuid.uuid4(), feedback_type="others")],
    )

    total, items = _run(
        factory.get_feedbacks(
            session,
            user_id=None,
            status=None,
            feedback_type=FeedbackType.OTHERS,
            skip=0,
            limit=5,
        )
    )

    assert total == 3
    assert len(items) == 1


def test_feedback_factory_initialize_short_circuit_when_already_initialized():
    factory = FeedbackFactory()
    factory._initialized = True

    # Should hit: if self._initialized: return
    factory.initialize()


def test_feedback_factory_create_feedback_sets_created_at_when_none():
    factory = FeedbackFactory()

    session = FakeCreateSession()
    feedback = _run(
        factory.create_feedback(
            session,
            feedback_id=uuid.uuid4(),
            user_id="usr",
            ticket_number="TKT-1",
            type=None,
            severity=None,
            description=None,
            location=None,
            attachments=None,
            status=Status.RECEIVED,
            created_at=None,
        )
    )

    assert feedback.created_at is not None
    assert feedback.updated_at is not None
    assert feedback.updated_at == feedback.created_at


def test_get_feedback_factory_singleton_creation_when_none():
    # Force module-level singleton to None so the `if _feedback_factory is None:` branch runs.
    feedback_factory_module._feedback_factory = None

    f1 = feedback_factory_module.get_feedback_factory()
    f2 = feedback_factory_module.get_feedback_factory()
    assert f1 is f2
