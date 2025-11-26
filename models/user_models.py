from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy import String, Text, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(Text, nullable=False)

    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

<<<<<<< HEAD
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
=======
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
>>>>>>> eb80a24 (feat: update user database implementation)
    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    # 这里 Python 属性名改成 relation，但数据库列名仍然叫 "relationship"
    relation: Mapped[Optional[str]] = mapped_column("relationship", Text, nullable=True)
    relation: Mapped[Optional[str]] = mapped_column(
        "relationship", Text, nullable=True
    )

    is_primary: Mapped[Optional[bool]] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="trusted_contacts")
