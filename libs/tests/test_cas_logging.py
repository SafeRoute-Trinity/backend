"""
CAS logging: structured JSON shape (pure unit tests — no Docker/DB/Redis).

Verify locally::

    pytest libs/tests/test_cas_logging.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

import pytest

from libs.cas_logger import Op, cas_log
from libs.structured_logging import AzureJsonFormatter, CAS_LOG_EXTRA_KEYS, setup_structured_logging
from libs.trace_context import trace_id_var


@pytest.fixture
def json_log_lines() -> Any:
    """Capture root logs as parsed JSON dicts; restores handlers after."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    records: List[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    setup_structured_logging("test_cas_service")
    capture = _Capture()
    capture.setFormatter(root.handlers[0].formatter)  # type: ignore[union-attr]
    root.handlers.clear()
    root.addHandler(capture)
    root.setLevel(logging.INFO)

    def _lines() -> List[Dict[str, Any]]:
        fmt = AzureJsonFormatter("test_cas_service")
        return [json.loads(fmt.format(r)) for r in records]

    yield _lines
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    records.clear()


def test_cas_log_only_chain_shape(json_log_lines) -> None:
    """Log-only path: instance_id, trace_id, cas_sequence; no cas_row_version."""
    trace_id_var.set("unit-trace-1")

    async def _run() -> None:
        await cas_log.begin(Op.FEEDBACK_VALIDATE, {"step": "test"})
        await cas_log.transition(Op.FEEDBACK_VALIDATE, "INIT", "VALIDATED")
        await cas_log.transition(Op.FEEDBACK_VALIDATE, "VALIDATED", "COMPLETED")

    asyncio.run(_run())

    lines = json_log_lines()
    assert len(lines) == 3
    for i, row in enumerate(lines, start=1):
        assert row["service"] == "test_cas_service"
        assert row["trace_id"] == "unit-trace-1"
        assert "instance_id" in row and row["instance_id"]
        assert row["cas_operation"] == "feedback_validate"
        assert row["cas_sequence"] == i
        assert "cas_row_version" not in row
        assert row["cas_valid"] is True
        assert row["level"] == "INFO"


def test_azure_formatter_merges_all_cas_extra_keys() -> None:
    """Formatter must pass through every CAS key cas_logger may set (contract)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        setup_structured_logging("fmt_test")
        fmt = root.handlers[0].formatter  # type: ignore[union-attr]
        record = logging.LogRecord(
            name="cas",
            level=logging.INFO,
            pathname="x",
            lineno=1,
            msg="CAS x: a -> b",
            args=(),
            exc_info=None,
        )
        record.cas_operation = "feedback_validate"
        record.cas_sequence = 1
        record.cas_expected_state = "INIT"
        record.cas_new_state = "VALIDATED"
        record.cas_payload_hash = "abc"
        record.cas_valid = True
        record.cas_detail = {"k": "v"}
        record.cas_row_version = 7
        record.cas_conflict = True
        out = json.loads(fmt.format(record))
        for key in CAS_LOG_EXTRA_KEYS:
            assert key in out
        assert out["cas_row_version"] == 7
        assert out["cas_conflict"] is True
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


class _FakeEnforcer:
    """In-memory stand-in: no DB/Redis; returns monotonic row versions."""

    ready = True
    _v = 0

    async def begin(self, operation, detail=None) -> int:
        type(self)._v = 1
        return 1

    async def transition(self, operation, expected, new, detail=None) -> int:
        type(self)._v += 1
        return type(self)._v


def test_cas_row_version_in_logs_when_enforcer_succeeds(json_log_lines) -> None:
    """Enforcer success → JSON includes cas_row_version (professor / KQL ordering)."""
    trace_id_var.set("unit-trace-2")
    _FakeEnforcer._v = 0
    cas_log.attach_enforcer(_FakeEnforcer())  # type: ignore[arg-type]

    async def _run() -> None:
        await cas_log.begin(Op.FEEDBACK_VALIDATE, {"fake": True})
        await cas_log.transition(Op.FEEDBACK_VALIDATE, "INIT", "VALIDATED")
        await cas_log.transition(Op.FEEDBACK_VALIDATE, "VALIDATED", "COMPLETED")

    asyncio.run(_run())
    cas_log.attach_enforcer(None)

    lines = json_log_lines()
    assert len(lines) == 3
    assert lines[0]["cas_row_version"] == 1
    assert lines[1]["cas_row_version"] == 2
    assert lines[2]["cas_row_version"] == 3
