from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from models.base import Base


class Emergency(Base):
    __tablename__ = "emergency"

    emergency_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("saferoute.users.user_id"),
        nullable=False,
        index=True,
    )

    route_id = Column(
        UUID(as_uuid=True),
        nullable=True,
    )

    lat = Column(
        Float,
        nullable=False,
    )

    lon = Column(
        Float,
        nullable=False,
    )

    trigger_type = Column(
        String(50),
        nullable=False,
    )

    messaging_id = Column(
        UUID(as_uuid=True),
        nullable=True,
    )

    message = Column(
        String(50),
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('manual', 'automatic')",
            name="chk_emergency_trigger_type",
        ),
        Index("idx_emergency_user_id", "user_id"),
        {"schema": "saferoute"},
    )
