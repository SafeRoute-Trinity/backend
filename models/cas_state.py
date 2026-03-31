"""
CAS (Compare-and-Swap) state table for distributed consistency enforcement.

Each row tracks one in-flight operation (identified by ``trace_id`` +
``operation``).  Replicas compete for state transitions via an atomic
``UPDATE … WHERE current_state = :expected`` — the database guarantees only
one writer succeeds, giving true compare-and-swap semantics without
application-level locking.

Schema creation SQL (run once per environment)::

    CREATE TABLE IF NOT EXISTS saferoute.cas_state (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        trace_id      VARCHAR(64)  NOT NULL,
        operation     VARCHAR(64)  NOT NULL,
        service       VARCHAR(64)  NOT NULL,
        current_state VARCHAR(64)  NOT NULL DEFAULT 'INIT',
        version       INTEGER      NOT NULL DEFAULT 1,
        payload_hash  VARCHAR(64),
        detail        JSONB,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        expires_at    TIMESTAMPTZ  NOT NULL DEFAULT now() + INTERVAL '24 hours',
        CONSTRAINT uq_cas_trace_op UNIQUE (trace_id, operation)
    );

    CREATE INDEX IF NOT EXISTS ix_cas_state_trace ON saferoute.cas_state (trace_id);
    CREATE INDEX IF NOT EXISTS ix_cas_state_expires ON saferoute.cas_state (expires_at);
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class CASState(Base):
    __tablename__ = "cas_state"
    __table_args__ = (
        UniqueConstraint("trace_id", "operation", name="uq_cas_trace_op"),
        {"schema": "saferoute"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    operation: Mapped[str] = mapped_column(String(64), nullable=False)

    service: Mapped[str] = mapped_column(String(64), nullable=False)

    current_state: Mapped[str] = mapped_column(String(64), nullable=False, default="INIT")

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now() + interval '24 hours'"),
        nullable=False,
    )
