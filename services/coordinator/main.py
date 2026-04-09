import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from libs.audit_logger import write_audit
from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import (
    CORSMiddlewareConfig,
    FastAPIServiceFactory,
    ServiceAppConfig,
)
from libs.outbox import ensure_outbox_tables
from libs.service_urls import NOTIFICATION_SERVICE_URL, SOS_SERVICE_URL
from libs.two_pc import (
    TX_ABORTED,
    TX_ABORTING,
    TX_COMMITTED,
    TX_COMMITTING,
    TX_PREPARED,
    TX_PREPARING,
    ensure_coordinator_tx_table,
)
from models.emergency import Emergency
from models.outbox import OutboxEvent
from models.user_models import (  # noqa: F401 (registers `saferoute.users` for FK resolution)
    User,
)

logger = logging.getLogger(__name__)

load_dotenv()

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


class EmergencyCallRequest(BaseModel):
    user_id: str
    route_id: Optional[uuid.UUID] = None
    lat: float
    lon: float
    trigger_type: Literal["manual", "automatic"]
    phone: str  # E.164 format, e.g. +353831234567


class EmergencyCallResponse(BaseModel):
    emergency_id: uuid.UUID
    status: Literal["initiated", "failed"]
    call_id: str
    timestamp: datetime


service_config = ServiceAppConfig(
    title="SafeRoute Coordinator Service",
    description="Coordinates DB atomicity + outbox publication for multi-service workflows.",
    service_name="coordinator",
    cors_config=CORSMiddlewareConfig(),
)

initialize_databases([DatabaseType.POSTGRES])

db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGRES)
get_serializable_db = db_factory.get_serializable_session_dependency(DatabaseType.POSTGRES)

factory = FastAPIServiceFactory(service_config)
app = factory.create_app()

SOS_CALLS_QUEUED_TOTAL = factory.add_business_metric(
    "sos_calls_queued_total",
    "Total SOS emergency call intents queued to outbox",
)


# Participants for the SOS 2PC. The coordinator drives prepare/commit/abort
# in the same order on every transaction. Order is purely cosmetic — phase
# calls are dispatched in parallel.
SOS_2PC_PARTICIPANTS = [
    {
        "name": "sos",
        "prepare": f"{SOS_SERVICE_URL}/v1/sos/2pc/prepare",
        "commit": f"{SOS_SERVICE_URL}/v1/sos/2pc/commit",
        "abort": f"{SOS_SERVICE_URL}/v1/sos/2pc/abort",
    },
    {
        "name": "notification",
        "prepare": f"{NOTIFICATION_SERVICE_URL}/v1/notifications/2pc/prepare",
        "commit": f"{NOTIFICATION_SERVICE_URL}/v1/notifications/2pc/commit",
        "abort": f"{NOTIFICATION_SERVICE_URL}/v1/notifications/2pc/abort",
    },
]

# Tunables. Tight by design — SOS is latency-sensitive.
PREPARE_TIMEOUT_SECONDS = 5.0
COMMIT_TIMEOUT_SECONDS = 5.0
COMMIT_MAX_RETRIES = 3
TX_EXPIRY_SECONDS = 60
RECOVERY_INTERVAL_SECONDS = 30


@app.on_event("startup")
async def startup_event() -> None:
    # DDL is idempotent; for unit tests/CI this might fail if no DB is present,
    # but the service can still start (worker paths will fail gracefully later).
    try:
        connection = db_factory.get_connection(DatabaseType.POSTGRES)
        async with connection.session_maker() as session:
            await ensure_outbox_tables(session)
            await ensure_coordinator_tx_table(session)
    except Exception:
        logger.exception("Failed to ensure outbox/coordinator_tx tables on startup")

    # Kick off the recovery loop. It first sweeps any in-flight transactions
    # left over from a previous coordinator crash, then runs periodically.
    if os.getenv("DISABLE_RECOVERY_LOOP", "false").lower() != "true":
        app.state.recovery_task = asyncio.create_task(_recovery_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    task = getattr(app.state, "recovery_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.post("/v1/coordinator/sos/call", response_model=EmergencyCallResponse)
async def queue_emergency_call(
    body: EmergencyCallRequest,
    db: AsyncSession = Depends(get_serializable_db),
):
    """
    Atomic operation:
      1) Insert Emergency row (+ audit)
      2) Insert outbox event to send the emergency call via Notification service

    These happen in a single DB transaction.
    """
    now = datetime.utcnow()
    emergency_id = uuid.uuid4()

    # Correlation id; may be replaced by Twilio SID later by the worker.
    call_request_id = f"CALL-{uuid.uuid4().hex[:10]}"
    call_reason = f"SOS {body.trigger_type}"

    try:
        db.add(
            Emergency(
                emergency_id=emergency_id,
                user_id=body.user_id,
                route_id=body.route_id,
                lat=body.lat,
                lon=body.lon,
                trigger_type=body.trigger_type,
                messaging_id=None,
                message=f"SOS {body.trigger_type}",
            )
        )

        # Best-effort audit write (must not break the coordinator transaction).
        # `write_audit` uses SAVEPOINT and can fall back if DB is unavailable.
        await write_audit(
            db=db,
            event_type="emergency",
            user_id=None,
            event_id=emergency_id,
            message=f"sos_call_queued emergency_id={emergency_id} trigger_type={body.trigger_type}",
            commit=False,
        )

        db.add(
            OutboxEvent(
                event_id=uuid.uuid4(),
                event_type="sos.emergency_call",
                aggregate_id=emergency_id,
                payload={
                    "emergency_id": str(emergency_id),
                    "user_id": body.user_id,
                    "phone_number": body.phone,
                    "user_location": {"lat": body.lat, "lon": body.lon},
                    "call_reason": call_reason,
                    "call_request_id": call_request_id,
                },
                status="pending",
                attempts=0,
                max_attempts=5,
                available_at=now,
                last_error=None,
            )
        )

        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Could not queue emergency call: {str(e)}",
        )
    except Exception:
        await db.rollback()
        raise

    SOS_CALLS_QUEUED_TOTAL.inc()

    return EmergencyCallResponse(
        emergency_id=emergency_id,
        status="initiated",
        call_id=call_request_id,
        timestamp=now,
    )


# ============================ 2PC Orchestration ============================
#
# /v1/coordinator/sos/2pc/call drives a textbook two-phase commit across the
# SOS and notification participants for a single emergency call.
#
# State machine (persisted in saferoute.coordinator_tx):
#
#     PREPARING ──(all YES)──> COMMITTING ──(all commits ack)──> COMMITTED
#         │                        │
#         │                        └──(commit ack failure)──> stays in
#         │                              COMMITTING; recovery loop retries
#         │                              (we never abort past commit point)
#         │
#         └──(any NO / timeout)──> ABORTING ──(aborts ack)──> ABORTED
#
# The transition into COMMITTING is the *commit point*: from that moment on
# the coordinator is contractually obliged to drive every participant to
# COMMITTED, retrying as long as it takes. The recovery loop on startup +
# periodic sweep enforces this.


def _participant_url(name: str, phase: str) -> str:
    for p in SOS_2PC_PARTICIPANTS:
        if p["name"] == name:
            return p[phase]
    raise KeyError(f"unknown participant {name}")


async def _set_tx_state(
    session: AsyncSession,
    tx_id: uuid.UUID,
    new_state: str,
    last_error: Optional[str] = None,
) -> None:
    await session.execute(
        text("""
            UPDATE saferoute.coordinator_tx
            SET state = :state,
                last_error = :err,
                updated_at = NOW()
            WHERE tx_id = :tx_id
            """),
        {"state": new_state, "err": last_error, "tx_id": str(tx_id)},
    )
    await session.commit()


async def _call_phase(
    client: httpx.AsyncClient,
    url: str,
    body: dict,
    timeout: float,
) -> tuple[bool, Optional[dict], Optional[str]]:
    """Call a single participant phase. Returns (ok, json_body, error_str)."""
    try:
        resp = await client.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        return True, resp.json(), None
    except httpx.HTTPError as e:
        return False, None, str(e)
    except Exception as e:  # pragma: no cover - belt and braces
        return False, None, repr(e)


async def _drive_prepare(
    client: httpx.AsyncClient, tx_id: uuid.UUID, payload: dict
) -> tuple[bool, list[str]]:
    """Phase 1: parallel prepare. Returns (all_yes, errors)."""
    body = {"tx_id": str(tx_id), "payload": payload, "expires_in_seconds": TX_EXPIRY_SECONDS}
    results = await asyncio.gather(
        *[
            _call_phase(client, p["prepare"], body, PREPARE_TIMEOUT_SECONDS)
            for p in SOS_2PC_PARTICIPANTS
        ],
        return_exceptions=False,
    )
    errors: list[str] = []
    all_yes = True
    for (ok, data, err), p in zip(results, SOS_2PC_PARTICIPANTS, strict=True):
        if not ok:
            all_yes = False
            errors.append(f"{p['name']}: {err}")
            continue
        if (data or {}).get("vote") != "YES":
            all_yes = False
            errors.append(f"{p['name']}: voted NO ({(data or {}).get('reason')})")
    return all_yes, errors


async def _drive_commit(
    client: httpx.AsyncClient, tx_id: uuid.UUID
) -> tuple[bool, dict[str, dict], list[str]]:
    """
    Phase 2 commit: parallel commit with bounded retries per participant.

    A failure here does NOT trigger an abort — once the commit point is
    reached, the only valid resolution is COMMITTED. We surface the error
    so the caller can leave the tx in COMMITTING for the recovery loop.
    """
    body = {"tx_id": str(tx_id)}

    async def _commit_one(p: dict) -> tuple[str, bool, Optional[dict], Optional[str]]:
        last_err: Optional[str] = None
        for attempt in range(COMMIT_MAX_RETRIES):
            ok, data, err = await _call_phase(client, p["commit"], body, COMMIT_TIMEOUT_SECONDS)
            if ok:
                return p["name"], True, data, None
            last_err = err
            await asyncio.sleep(0.2 * (2**attempt))
        return p["name"], False, None, last_err

    results = await asyncio.gather(*[_commit_one(p) for p in SOS_2PC_PARTICIPANTS])
    per_participant: dict[str, dict] = {}
    errors: list[str] = []
    all_ok = True
    for name, ok, data, err in results:
        if ok:
            per_participant[name] = (data or {}).get("result", {}) or {}
        else:
            all_ok = False
            errors.append(f"{name}: {err}")
    return all_ok, per_participant, errors


async def _drive_abort(client: httpx.AsyncClient, tx_id: uuid.UUID, reason: str) -> list[str]:
    """Phase 2 abort: best-effort parallel. Errors are logged, not raised."""
    body = {"tx_id": str(tx_id), "reason": reason}
    results = await asyncio.gather(
        *[
            _call_phase(client, p["abort"], body, COMMIT_TIMEOUT_SECONDS)
            for p in SOS_2PC_PARTICIPANTS
        ],
        return_exceptions=False,
    )
    errors: list[str] = []
    for (ok, _data, err), p in zip(results, SOS_2PC_PARTICIPANTS, strict=True):
        if not ok:
            errors.append(f"{p['name']}: {err}")
    return errors


@app.post("/v1/coordinator/sos/2pc/call", response_model=EmergencyCallResponse)
async def two_pc_sos_call(
    body: EmergencyCallRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Drive a 2PC across SOS + notification for a single emergency call.

    Returns 200 only if the tx reaches COMMITTED. A coordinator that crashes
    mid-flight leaves a non-terminal coordinator_tx row that the startup
    recovery loop will replay.
    """
    tx_id = uuid.uuid4()
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=TX_EXPIRY_SECONDS)
    payload = body.model_dump(mode="json")
    participants_meta = [{"name": p["name"]} for p in SOS_2PC_PARTICIPANTS]

    # Step 0: durably record the tx in PREPARING before doing any RPC.
    await db.execute(
        text("""
            INSERT INTO saferoute.coordinator_tx
                (tx_id, state, participants, payload, expires_at)
            VALUES
                (:tx_id, :state, CAST(:participants AS JSONB),
                 CAST(:payload AS JSONB), :expires_at)
            """),
        {
            "tx_id": str(tx_id),
            "state": TX_PREPARING,
            "participants": json.dumps(participants_meta),
            "payload": json.dumps(payload),
            "expires_at": expires_at,
        },
    )
    await db.commit()

    async with httpx.AsyncClient() as client:
        # ===== Phase 1: prepare =====
        all_yes, prep_errors = await _drive_prepare(client, tx_id, payload)
        if not all_yes:
            await _set_tx_state(db, tx_id, TX_ABORTING, "; ".join(prep_errors))
            abort_errors = await _drive_abort(client, tx_id, reason="prepare phase failed")
            await _set_tx_state(
                db,
                tx_id,
                TX_ABORTED,
                "; ".join(prep_errors + abort_errors) or None,
            )
            raise HTTPException(
                status_code=409,
                detail=f"2PC prepare failed: {'; '.join(prep_errors)}",
            )

        await _set_tx_state(db, tx_id, TX_PREPARED)

        # ===== Commit point =====
        await _set_tx_state(db, tx_id, TX_COMMITTING)

        # ===== Phase 2: commit =====
        all_ok, per_participant, commit_errors = await _drive_commit(client, tx_id)
        if not all_ok:
            # Stay in COMMITTING. Recovery loop will retry until success.
            await _set_tx_state(db, tx_id, TX_COMMITTING, "; ".join(commit_errors))
            logger.error(
                "2PC commit incomplete tx_id=%s errors=%s — recovery will retry",
                tx_id,
                commit_errors,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "2PC commit incomplete; transaction will be reconciled by "
                    f"recovery loop (tx_id={tx_id})"
                ),
            )

        await _set_tx_state(db, tx_id, TX_COMMITTED)

    SOS_CALLS_QUEUED_TOTAL.inc()

    sos_result = per_participant.get("sos", {})
    emergency_id_str = sos_result.get("emergency_id") or str(tx_id)
    call_id = sos_result.get("call_id") or f"CALL-{emergency_id_str}"
    try:
        emergency_id = uuid.UUID(emergency_id_str)
    except Exception:
        emergency_id = tx_id

    return EmergencyCallResponse(
        emergency_id=emergency_id,
        status="initiated",
        call_id=call_id,
        timestamp=datetime.utcnow(),
    )


# ============================ Recovery Loop ============================
#
# Drives stuck transactions to a terminal state. Runs once at startup
# (catching crash-recovery cases) and then periodically.


async def _recover_one(client: httpx.AsyncClient, tx_id: uuid.UUID, state: str) -> None:
    """Resolve a single non-terminal tx to a terminal state."""
    connection = db_factory.get_connection(DatabaseType.POSTGRES)
    async with connection.session_maker() as session:
        if state in (TX_PREPARING, TX_ABORTING):
            # Safe to abort: PREPARING never reached the commit point.
            errors = await _drive_abort(client, tx_id, reason="recovery sweep")
            await _set_tx_state(session, tx_id, TX_ABORTED, "; ".join(errors) or None)
            logger.info("recovery: aborted tx_id=%s (was %s)", tx_id, state)
            return

        if state in (TX_PREPARED, TX_COMMITTING):
            # Past the commit point — must drive forward to COMMITTED.
            all_ok, _result, errors = await _drive_commit(client, tx_id)
            if all_ok:
                await _set_tx_state(session, tx_id, TX_COMMITTED)
                logger.info("recovery: committed tx_id=%s (was %s)", tx_id, state)
            else:
                await _set_tx_state(session, tx_id, TX_COMMITTING, "; ".join(errors) or None)
                logger.warning("recovery: tx_id=%s still failing commit: %s", tx_id, errors)
            return


async def _recovery_sweep() -> None:
    connection = db_factory.get_connection(DatabaseType.POSTGRES)
    async with connection.session_maker() as session:
        rows = (await session.execute(text("""
                    SELECT tx_id, state
                    FROM saferoute.coordinator_tx
                    WHERE state NOT IN ('COMMITTED', 'ABORTED')
                    ORDER BY created_at
                    LIMIT 100
                    """))).fetchall()

    if not rows:
        return

    logger.info("recovery sweep: %d non-terminal tx to resolve", len(rows))
    async with httpx.AsyncClient() as client:
        for row in rows:
            tx_id_val, state = row[0], row[1]
            try:
                tx_id = tx_id_val if isinstance(tx_id_val, uuid.UUID) else uuid.UUID(str(tx_id_val))
                await _recover_one(client, tx_id, state)
            except Exception:
                logger.exception("recovery: failed to resolve tx_id=%s", tx_id_val)


async def _recovery_loop() -> None:
    # First pass runs immediately on startup to handle crash-recovery.
    try:
        await _recovery_sweep()
    except Exception:
        logger.exception("initial recovery sweep failed")

    while True:
        try:
            await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
            await _recovery_sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recovery loop iteration failed")
