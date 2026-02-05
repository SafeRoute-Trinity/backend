# models/audit.py
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AuditEventType(str, enum.Enum):
    authentication = "authentication"
    routing = "routing"
    emergency = "emergency"
    notification = "notification"
    feedback = "feedback"


class Base(DeclarativeBase):
    pass


class Audit(Base):
    __tablename__ = "audit"
    __table_args__ = {"schema": "saferoute"}

    log_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        enum.Enum(AuditEventType, name="audit_event_type", native_enum=True),
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
