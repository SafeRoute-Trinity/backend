"""
Feedback-related database models for SafeRoute backend.

Defines SQLAlchemy ORM models for feedback submissions.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class Feedback(Base):
    """Feedback submission model."""

    __tablename__ = "feedback"
    __table_args__ = {"schema": "saferoute"}

    feedback_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )

    user_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    route_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")

    ticket_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, unique=True)

    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default="now()",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default="now()",
    )

    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Additional columns that exist in the database
    location: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    attachments: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
