# models/audit.py
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base

AuditEventType = Literal[
    "authentication",
    "routing",
    "emergency",
    "notification",
    "feedback",
]


class Audit(Base):
    __tablename__ = "audit"
    __table_args__ = {"schema": "saferoute"}

    log_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    message: Mapped[str] = mapped_column(Text, nullable=False)

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
