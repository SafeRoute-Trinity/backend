from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class OutboxEvent(Base):
    """
    Outbox table for reliable cross-service side-effect publication.

    The coordinator service writes:
      - domain rows (e.g., Emergency)
      - an outbox event describing the side-effect
    in a single DB transaction.

    A worker service later consumes outbox events and performs the side-effect.
    """

    __tablename__ = "outbox"
    __table_args__ = {"schema": "saferoute"}

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    aggregate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # pending -> processing -> done OR pending (retry) OR failed (dead-letter)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
