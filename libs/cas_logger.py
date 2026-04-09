"""
CAS (Compare-and-Swap) logging and state-machine validation for SafeRoute.

Requirements (what this module delivers)
----------------------------------------
- **Structured audit trail**: Every transition is one JSON log line (via the
  root logger + ``libs.structured_logging.AzureJsonFormatter``) suitable for
  Azure Log Analytics / KQL: ``trace_id``, ``instance_id``, ``cas_*`` fields.
- **Replica visibility**: ``instance_id`` is added by the formatter from
  ``POD_NAME`` / ``HOSTNAME`` (Kubernetes) or the machine hostname locally.
- **Cross-replica ordering**: When the DB enforcer succeeds, logs include
  ``cas_row_version`` (the ``version`` column in ``saferoute.cas_state``) so
  you can sort events per ``trace_id`` consistently across pods.
- **Conflicts**: Losing replica logs ``cas_conflict`` + ``[CONFLICT]`` before
  ``CASConflictError`` propagates (HTTP 409 from the factory middleware).

Tech stack alignment
--------------------
- **FastAPI**: Services call ``cas_log.begin`` / ``transition`` inside handlers;
  ``trace_id`` comes from ``libs.trace_context`` (``X-Trace-ID``).
- **PostgreSQL / PostGIS**: Persistence is optional at log time; when
  ``CASEnforcer`` is attached (see ``libs.fastapi_service`` startup), rows
  live in ``saferoute.cas_state``. URL resolution is in
  ``libs.cas_enforcer.resolve_cas_database_url`` (PostGIS when ``POSTGIS_HOST``
  or ``POSTGIS_DATABASE_URL`` is set).
- **Redis**: Pub/sub on ``cas:state_changes`` is best-effort; enforcer still
  runs if Redis is down (warning only).

Usage::

    from libs.cas_logger import cas_log, Op

    await cas_log.begin(Op.EMERGENCY_CALL, detail={"user_id": uid})
    await cas_log.transition(Op.EMERGENCY_CALL, "INIT", "EMERGENCY_CREATED",
                             detail={"emergency_id": str(eid)})

KQL sketch::

    ContainerLog
    | extend p = parse_json(LogEntry)
    | where isnotempty(tostring(p.cas_operation))
    | where p.trace_id == "<id>"
    | order by toint(p.cas_row_version) asc, toint(p.cas_sequence) asc
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

if TYPE_CHECKING:
    from libs.cas_enforcer import CASEnforcer

logger = logging.getLogger("cas")

_cas_sequence: ContextVar[int] = ContextVar("cas_sequence", default=0)


class Op(str, Enum):
    """Known multi-step operations across the five services."""

    EMERGENCY_CALL = "emergency_call"
    EMERGENCY_SMS = "emergency_sms"
    FEEDBACK_SUBMIT = "feedback_submit"
    FEEDBACK_VALIDATE = "feedback_validate"
    SYSTEM_FEEDBACK = "system_feedback"
    USER_SYNC = "user_sync"
    USER_PROFILE_FETCH = "user_profile_fetch"
    PREFERENCES_SAVE = "preferences_save"
    TRUSTED_CONTACT_UPSERT = "trusted_contact_upsert"
    ROUTE_CALCULATE = "route_calculate"
    NAVIGATION_START = "navigation_start"
    SAFETY_ROUTE = "safety_route"
    SAFETY_WEIGHT_UPDATE = "safety_weight_update"


_STATE_MACHINES: Dict[Op, Dict[str, Set[str]]] = {
    Op.EMERGENCY_CALL: {
        "INIT": {"EMERGENCY_CREATED"},
        "EMERGENCY_CREATED": {"CONTACT_FETCHED"},
        "CONTACT_FETCHED": {"NOTIFICATION_REQUESTED"},
        "NOTIFICATION_REQUESTED": {"NOTIFICATION_SENT", "NOTIFICATION_FAILED"},
        "NOTIFICATION_SENT": {"COMMITTED"},
        "NOTIFICATION_FAILED": {"FAILED"},
    },
    Op.EMERGENCY_SMS: {
        "INIT": {"VALIDATED"},
        "VALIDATED": {"NOTIFICATION_REQUESTED"},
        "NOTIFICATION_REQUESTED": {"SMS_SENT", "NOTIFICATION_FAILED"},
        "SMS_SENT": {"COMMITTED"},
        "NOTIFICATION_FAILED": {"FAILED"},
    },
    Op.FEEDBACK_SUBMIT: {
        "INIT": {"VALIDATED"},
        "VALIDATED": {"DB_CREATED"},
        "DB_CREATED": {"COMMITTED", "DB_FAILED"},
        "COMMITTED": {"COMPLETED"},
        "DB_FAILED": {"FAILED"},
    },
    Op.FEEDBACK_VALIDATE: {
        "INIT": {"VALIDATED"},
        "VALIDATED": {"COMPLETED"},
    },
    Op.SYSTEM_FEEDBACK: {
        "INIT": {"CAPTCHA_VERIFIED"},
        "CAPTCHA_VERIFIED": {"EMAIL_SENT", "EMAIL_FAILED"},
        "EMAIL_SENT": {"COMPLETED"},
        "EMAIL_FAILED": {"FAILED"},
    },
    Op.USER_SYNC: {
        "INIT": {"SECRET_VERIFIED"},
        "SECRET_VERIFIED": {"USER_UPSERTED"},
        "USER_UPSERTED": {"COMMITTED", "COMMIT_FAILED"},
        "COMMITTED": {"COMPLETED"},
        "COMMIT_FAILED": {"FAILED"},
    },
    Op.USER_PROFILE_FETCH: {
        "INIT": {"TOKEN_VERIFIED"},
        "TOKEN_VERIFIED": {"USER_FOUND", "USER_NOT_FOUND"},
        "USER_NOT_FOUND": {"PROFILE_FETCHED"},
        "PROFILE_FETCHED": {"USER_CREATED"},
        "USER_CREATED": {"COMMITTED", "COMMIT_FAILED"},
        "USER_FOUND": {"COMPLETED"},
        "COMMITTED": {"COMPLETED"},
        "COMMIT_FAILED": {"FAILED"},
    },
    Op.PREFERENCES_SAVE: {
        "INIT": {"USER_VERIFIED"},
        "USER_VERIFIED": {"PREFERENCES_UPSERTED"},
        "PREFERENCES_UPSERTED": {"COMMITTED", "COMMIT_FAILED"},
        "COMMITTED": {"COMPLETED"},
        "COMMIT_FAILED": {"FAILED"},
    },
    Op.TRUSTED_CONTACT_UPSERT: {
        "INIT": {"USER_VERIFIED"},
        "USER_VERIFIED": {"CONTACT_UPSERTED"},
        "CONTACT_UPSERTED": {"COMMITTED", "COMMIT_FAILED"},
        "COMMITTED": {"COMPLETED"},
        "COMMIT_FAILED": {"FAILED"},
    },
    Op.ROUTE_CALCULATE: {
        "INIT": {"ROUTE_COMPUTED", "ROUTE_FALLBACK"},
        "ROUTE_COMPUTED": {"COMMITTED"},
        "ROUTE_FALLBACK": {"COMMITTED"},
        "COMMITTED": {"COMPLETED"},
    },
    Op.NAVIGATION_START: {
        "INIT": {"SESSION_CREATED"},
        "SESSION_CREATED": {"COMMITTED"},
        "COMMITTED": {"COMPLETED"},
    },
    Op.SAFETY_ROUTE: {
        "INIT": {"CH_REQUESTED", "DIJKSTRA_REQUESTED"},
        "CH_REQUESTED": {"ROUTE_COMPUTED", "CH_FAILED"},
        "CH_FAILED": {"DIJKSTRA_REQUESTED"},
        "DIJKSTRA_REQUESTED": {"ROUTE_COMPUTED", "NO_PATH"},
        "ROUTE_COMPUTED": {"COMPLETED"},
        "NO_PATH": {"FAILED"},
    },
    Op.SAFETY_WEIGHT_UPDATE: {
        "INIT": {"EDGE_FOUND", "EDGE_NOT_FOUND"},
        "EDGE_FOUND": {"UPDATED"},
        "UPDATED": {"COMMITTED"},
        "COMMITTED": {"COMPLETED"},
        "EDGE_NOT_FOUND": {"FAILED"},
    },
}


def _next_seq() -> int:
    seq = _cas_sequence.get(0) + 1
    _cas_sequence.set(seq)
    return seq


def _norm_row_version(v: Optional[int]) -> Optional[int]:
    """Enforcer returns 0 when skipped; omit from logs."""
    if v is None or v <= 0:
        return None
    return v


def _payload_hash(detail: Optional[Dict[str, Any]]) -> str:
    if not detail:
        return ""
    raw = json.dumps(detail, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_valid(op: Op, expected: str, new: str) -> bool:
    machine = _STATE_MACHINES.get(op)
    if not machine:
        return True
    targets = machine.get(expected)
    if targets is None:
        return expected == "NONE" and new == "INIT"
    return new in targets


class CASLogger:
    """
    Async CAS state-transition logger with optional ``CASEnforcer``.

    Log fields on the ``LogRecord`` must stay in sync with
    ``libs.structured_logging.CAS_LOG_EXTRA_KEYS`` (pytest enforces the contract).
    """

    def __init__(self) -> None:
        self._enforcer: Optional[CASEnforcer] = None

    def attach_enforcer(self, enforcer: Optional[CASEnforcer]) -> None:
        """Wire the DB-backed enforcer at startup, or ``None`` to detach (e.g. tests)."""
        self._enforcer = enforcer

    async def begin(
        self,
        operation: Op,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Reset sequence, persist INIT when enforcer is ready, then emit one log line."""
        _cas_sequence.set(0)
        row_version: Optional[int] = None
        if self._enforcer and self._enforcer.ready:
            try:
                row_version = await self._enforcer.begin(operation, detail)
            except Exception:
                logger.warning(
                    "CAS enforcer begin failed (table missing or DB down?) — logging only",
                    exc_info=True,
                )
        self._emit(
            operation,
            "NONE",
            "INIT",
            detail,
            cas_row_version=_norm_row_version(row_version),
        )

    async def transition(
        self,
        operation: Op,
        expected: str,
        new: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist transition when enforcer is ready, then emit with DB row version."""
        from libs.cas_enforcer import CASConflictError

        enforcer = self._enforcer if self._enforcer and self._enforcer.ready else None

        if not _is_valid(operation, expected, new):
            self._emit(operation, expected, new, detail)
            if enforcer:
                try:
                    await enforcer.transition(operation, expected, new, detail)
                except CASConflictError:
                    raise
                except Exception:
                    logger.warning(
                        "CAS enforcer transition failed (%s -> %s) — logging only",
                        expected,
                        new,
                        exc_info=True,
                    )
            return

        row_version: Optional[int] = None
        if enforcer:
            try:
                row_version = await enforcer.transition(operation, expected, new, detail)
            except CASConflictError:
                self._emit(
                    operation,
                    expected,
                    new,
                    detail,
                    cas_conflict=True,
                )
                raise
            except Exception:
                logger.warning(
                    "CAS enforcer transition failed (%s -> %s) — logging only",
                    expected,
                    new,
                    exc_info=True,
                )

        self._emit(
            operation,
            expected,
            new,
            detail,
            cas_row_version=_norm_row_version(row_version),
        )

    def _emit(
        self,
        operation: Op,
        expected: str,
        new: str,
        detail: Optional[Dict[str, Any]],
        *,
        cas_row_version: Optional[int] = None,
        cas_conflict: bool = False,
    ) -> None:
        """Write the structured log line (always, regardless of enforcer)."""
        seq = _next_seq()
        valid = _is_valid(operation, expected, new)

        extra: Dict[str, Any] = {
            "cas_operation": operation.value,
            "cas_sequence": seq,
            "cas_expected_state": expected,
            "cas_new_state": new,
            "cas_payload_hash": _payload_hash(detail),
            "cas_valid": valid,
        }
        if detail:
            extra["cas_detail"] = detail
        if cas_row_version is not None:
            extra["cas_row_version"] = cas_row_version
        if cas_conflict:
            extra["cas_conflict"] = True

        msg = f"CAS {operation.value}: {expected} -> {new}"

        if cas_conflict:
            logger.warning(msg + " [CONFLICT]", extra=extra)
        elif valid:
            logger.info(msg, extra=extra)
        else:
            logger.warning(msg + " [INVALID TRANSITION]", extra=extra)


cas_log = CASLogger()
