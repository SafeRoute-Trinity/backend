"""
User-related database models for SafeRoute backend.

Defines SQLAlchemy ORM models for users, preferences, and trusted contacts.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class User(Base):
    """
    User model representing a SafeRoute application user.

    Attributes:
        user_id: Unique identifier for the user (primary key)
        email: User's email address (unique, required)
        password_hash: Hashed password (required)
        device_id: Device identifier (required)
        phone: User's phone number (optional)
        name: User's display name (optional)
        created_at: Timestamp when user account was created
        last_login: Timestamp of last login (optional)
        preferences: User preferences relationship
        trusted_contacts: List of trusted contacts relationship
    """

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)

    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    preferences: Mapped[Optional["UserPreferences"]] = relationship(
        back_populates="user",
        uselist=False,
    )
    trusted_contacts: Mapped[List["TrustedContact"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserPreferences(Base):
    """
    User preferences model for storing user settings.

    Attributes:
        user_id: Foreign key to users table (primary key)
        voice_guidance: Voice guidance setting (on/off)
        safety_bias: Route preference (safest/fastest, optional)
        units: Measurement units (metric/imperial, optional)
        updated_at: Timestamp when preferences were last updated
        user: Relationship back to User model
    """

    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )

    voice_guidance: Mapped[str] = mapped_column(Text, nullable=False)
    safety_bias: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    units: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="preferences")


class TrustedContact(Base):
    """
    Trusted contact model for emergency contacts.

    Attributes:
        contact_id: Unique identifier for the contact (primary key)
        user_id: Foreign key to users table (indexed)
        name: Contact's name (required)
        phone: Contact's phone number (required)
        relation: Relationship to user (e.g., "family", "friend", optional)
        is_primary: Whether this is the primary emergency contact (optional)
        created_at: Timestamp when contact was created
        updated_at: Timestamp when contact was last updated
        user: Relationship back to User model
    """

    __tablename__ = "trusted_contacts"

    contact_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    user_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)

    # Python attribute name is 'relation', but database column is 'relationship'
    relation: Mapped[Optional[str]] = mapped_column("relationship", Text, nullable=True)

    is_primary: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="trusted_contacts")
