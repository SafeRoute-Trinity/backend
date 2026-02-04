# models/audit.py
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# from models.base import Base


class Base(DeclarativeBase):
    pass


class Audit(Base):
    """
    ORM model for table: saferoute.audit
    """

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

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    created_at: Mapped[object] = mapped_column(
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[object] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
