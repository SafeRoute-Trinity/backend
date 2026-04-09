"""
Two-Phase Commit (2PC) support for SOS.

This module provides:
  * DDL helpers for the coordinator + per-participant transaction tables
  * Shared Pydantic request/response models used by the prepare/commit/abort
    endpoints on every participant
  * Tx state constants

Why 2PC for SOS?
  SOS is a critical, multi-service write (emergency record + notification
  dispatch). The transactional outbox already in this repo gives us local
  atomicity but not cross-service agreement. 2PC adds a durable "did all
  participants vote yes?" decision so a half-finished SOS cannot happen.

Design notes:
  * Coordinator persists `coordinator_tx` rows and is the sole authority on
    the COMMIT/ABORT decision (the row transition to COMMITTING is the
    commit point).
  * Each participant persists its own `*_participant_tx` row holding the
    tentative payload. The real domain row (e.g. `Emergency`) is only
    materialized on commit. This avoids any schema change to existing
    domain tables and keeps the tentative state cleanly separable.
  * Recovery: on coordinator startup, non-terminal coordinator_tx rows are
    replayed (PREPARING -> abort, COMMITTING -> commit, ABORTING -> abort).
    Participant endpoints are idempotent on tx_id so replay is safe.
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ----- Coordinator-side states -----
TX_PREPARING = "PREPARING"
TX_PREPARED = "PREPARED"
TX_COMMITTING = "COMMITTING"  # the commit point
TX_COMMITTED = "COMMITTED"
TX_ABORTING = "ABORTING"
TX_ABORTED = "ABORTED"

COORDINATOR_TERMINAL_STATES = {TX_COMMITTED, TX_ABORTED}

# ----- Participant-side states -----
P_PREPARED = "PREPARED"
P_COMMITTED = "COMMITTED"
P_ABORTED = "ABORTED"


# ----- Wire types shared by all participants -----
class PrepareRequest(BaseModel):
    tx_id: UUID
    payload: dict[str, Any]
    expires_in_seconds: int = 60


class PrepareResponse(BaseModel):
    tx_id: UUID
    vote: Literal["YES", "NO"]
    reason: Optional[str] = None


class CommitRequest(BaseModel):
    tx_id: UUID


class CommitResponse(BaseModel):
    tx_id: UUID
    status: Literal["committed"]
    # Participant-specific identifiers (e.g. emergency_id) come back here.
    result: dict[str, Any] = {}


class AbortRequest(BaseModel):
    tx_id: UUID
    reason: Optional[str] = None


class AbortResponse(BaseModel):
    tx_id: UUID
    status: Literal["aborted"]


# ----- DDL helpers -----
async def ensure_coordinator_tx_table(session: AsyncSession) -> None:
    """Create the coordinator-side transaction log. Idempotent."""
    await session.execute(text("CREATE SCHEMA IF NOT EXISTS saferoute"))
    await session.execute(text("""
            CREATE TABLE IF NOT EXISTS saferoute.coordinator_tx (
                tx_id        UUID PRIMARY KEY,
                state        VARCHAR(20) NOT NULL,
                participants JSONB       NOT NULL,
                payload      JSONB       NOT NULL,
                last_error   TEXT        NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at   TIMESTAMPTZ NOT NULL
            )
            """))
    await session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_coordinator_tx_state "
            "ON saferoute.coordinator_tx (state)"
        )
    )
    await session.commit()


async def ensure_participant_tx_table(session: AsyncSession, table: str) -> None:
    """
    Create a participant-side transaction log. Idempotent.

    `table` should be a short identifier like 'sos_participant_tx' or
    'notif_participant_tx'. It is interpolated into DDL, so callers must
    only pass trusted constants (never user input).
    """
    if not table.replace("_", "").isalnum():
        raise ValueError(f"unsafe participant table name: {table}")

    await session.execute(text("CREATE SCHEMA IF NOT EXISTS saferoute"))
    await session.execute(text(f"""
            CREATE TABLE IF NOT EXISTS saferoute.{table} (
                tx_id      UUID PRIMARY KEY,
                state      VARCHAR(20) NOT NULL,
                payload    JSONB       NOT NULL,
                result     JSONB       NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL
            )
            """))
    await session.execute(
        text(f"CREATE INDEX IF NOT EXISTS idx_{table}_state " f"ON saferoute.{table} (state)")
    )
    await session.commit()
