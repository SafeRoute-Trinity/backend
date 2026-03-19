# libs/audit_logger.py
from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

import common.storage as _storage
from models.audit import Audit

logger = logging.getLogger(__name__)

# 建议统一的 event_type（和你们 7 个点一一对应）
ALLOWED_EVENT_TYPES = {
    "authentication",
    "user_management",
    "routing",
    "emergency",
    "notification",
    "feedback",
    "system",
}


def _normalize_event_type(event_type: str) -> str:
    et = (event_type or "").strip()
    if len(et) > 50:
        et = et[:50]
    return et


async def write_audit(
    *,
    db: AsyncSession,
    event_type: str,
    message: str,
    user_id: Optional[str] = None,
    event_id: Optional[uuid.UUID] = None,
    commit: bool = False,
) -> Optional[uuid.UUID]:
    """
    Write an audit record.

    Args:
        db: AsyncSession (通常就是 Depends(get_db) 拿到的那个)
        event_type: authentication / routing / emergency / ...
        message: human-readable message (NOT NULL)
        user_id: who triggered the event (nullable)
        event_id: affected entity id (route_id / emergency_id / ...)
        commit: 是否在这里直接 commit（默认 False，推荐）

    Returns:
        log_id (UUID) on success, None on failure
    """
    if db is None:
        raise ValueError("write_audit requires an AsyncSession")

    et = _normalize_event_type(event_type)
    if et not in ALLOWED_EVENT_TYPES:
        logger.warning("Unknown audit event_type '%s', still logging.", et)

    msg = (message or "").strip() or "(no message)"

    audit_row = Audit(
        user_id=user_id,
        event_type=et,
        event_id=event_id,
        message=msg,
    )

    # Wrap the audit INSERT in a SAVEPOINT so that a write failure only rolls
    # back the audit row, leaving any surrounding business-logic writes intact.
    # This replaces the old pattern of calling db.rollback() on audit failure,
    # which would have wiped the entire session's pending work.
    log_id: Optional[uuid.UUID] = None
    try:
        async with db.begin_nested():
            db.add(audit_row)
            # flush assigns log_id via the Python-side default (uuid.uuid4)
            await db.flush()
        log_id = audit_row.log_id
    except Exception as exc:
        # Audit failure must not affect the caller's business transaction.
        logger.exception(
            "Audit write failed: event_type=%s user_id=%s event_id=%s error=%s",
            et,
            str(user_id) if user_id else None,
            str(event_id) if event_id else None,
            repr(exc),
        )
        # Fallback: keep the audit in an in-memory store so we don't lose it when DB is down.
        try:
            _storage.audit_logs.append(
                {
                    "event_type": et,
                    "user_id": str(user_id) if user_id else None,
                    "event_id": str(event_id) if event_id else None,
                    "message": msg,
                    "error": repr(exc),
                }
            )
        except Exception:
            logger.exception("Failed to append audit to in-memory fallback")
        return None

    if commit:
        await db.commit()

    return log_id
