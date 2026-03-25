"""
CAS (Compare-and-Swap) Logger for distributed consistency management.

Emits structured state-transition log entries that form a verifiable chain
per ``trace_id``.  Each transition records the expected previous state and
the new state, creating an auditable sequence that Azure Monitor / KQL can
query to detect broken chains, skipped steps, or stuck operations.

Usage::

    from libs.cas_logger import cas_log, Op

    cas_log.begin(Op.EMERGENCY_CALL, detail={"user_id": uid})
    cas_log.transition(Op.EMERGENCY_CALL, "INIT", "EMERGENCY_CREATED",
                       detail={"emergency_id": str(eid)})

KQL consistency check::

    ContainerLog
    | extend p = parse_json(LogEntry)
    | where isnotempty(tostring(p.cas_operation))
    | where p.trace_id == "<id>"
    | order by toint(p.cas_sequence) asc
    | extend prev = prev(tostring(p.cas_new_state))
    | where isnotempty(prev) and tostring(p.cas_expected_state) != prev
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextvars import ContextVar
from enum import Enum
from typing import Any, Dict, Optional, Set

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
    """Singleton CAS state-transition logger."""

    def begin(
        self,
        operation: Op,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Reset sequence and emit the INIT marker for a new operation."""
        _cas_sequence.set(0)
        self.transition(operation, "NONE", "INIT", detail)

    def transition(
        self,
        operation: Op,
        expected: str,
        new: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a single state transition with CAS metadata."""
        seq = _next_seq()
        valid = _is_valid(operation, expected, new)

        extra = {
            "cas_operation": operation.value,
            "cas_sequence": seq,
            "cas_expected_state": expected,
            "cas_new_state": new,
            "cas_payload_hash": _payload_hash(detail),
            "cas_valid": valid,
        }
        if detail:
            extra["cas_detail"] = detail

        msg = f"CAS {operation.value}: {expected} -> {new}"

        if valid:
            logger.info(msg, extra=extra)
        else:
            logger.warning(msg + " [INVALID TRANSITION]", extra=extra)


cas_log = CASLogger()
