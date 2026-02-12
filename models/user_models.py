"""
User-related database models for SafeRoute backend.

Defines SQLAlchemy ORM models for users, preferences, and trusted contacts.
"""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (({"schema": "saferoute"}),)

    # DB has gen_random_uuid, but I prefer create uuid in backend because audit may use it
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=Text("now()"),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=Text("now()"),
    )

    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    preferences: Mapped[Optional["UserPreferences"]] = relationship(
        back_populates="user",
        uselist=False,
    )

    trusted_contacts: Mapped[List["TrustedContact"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserPreferences(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "units IN ('metric', 'imperial')",
            name="chk_user_preferences_units",
        ),
        {"schema": "saferoute"},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("saferoute.users.user_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )

    voice_guidance: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=Text("true"),
    )

    units: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=Text("'metric'"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=Text("now()"),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=Text("now()"),
    )

    user: Mapped["User"] = relationship(
        back_populates="preferences",
        lazy="selectin",
    )


class TrustedContact(Base):
    """
    ORM for DB table: saferoute.contacts
    """

    __tablename__ = "contacts"
    __table_args__ = {"schema": "saferoute"}

    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        server_default=Text("gen_random_uuid()"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("saferoute.users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,  # 對應 idx_contacts_user_id
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)

    # DB column is "relationship" varchar(50) nullable
    relation: Mapped[Optional[str]] = mapped_column(
        "relationship",
        String(50),
        nullable=True,
    )

    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=Text("false"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=Text("now()"),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=Text("now()"),
    )

    user: Mapped["User"] = relationship(
        back_populates="trusted_contacts",
        lazy="selectin",
    )
