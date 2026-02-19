"""
Feedback Factory - Database operations for feedback submissions.

Provides factory pattern for creating and managing feedback records in the database.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.feedback import Feedback
from services.feedback.types import FeedbackType, SeverityType, Status

logger = logging.getLogger(__name__)


class FeedbackFactory:
    """
    Factory for creating and managing Feedback database records.

    Follows singleton pattern similar to DatabaseFactory and SpamValidatorFactory.
    """

    _instance: Optional["FeedbackFactory"] = None
    _initialized: bool = False

    def __new__(cls):
        """Singleton pattern - ensures only one factory instance exists."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize factory (only runs once due to singleton)."""
        if not hasattr(self, "_initialized_instance"):
            self._initialized_instance = True
            self._initialized = False

    def initialize(self) -> None:
        """
        Initialize the factory.

        This should be called during application startup.
        """
        if self._initialized:
            return
        self._initialized = True
        logger.info("FeedbackFactory initialized successfully")

    async def create_feedback(
        self,
        db: AsyncSession,
        *,
        feedback_id: uuid.UUID,
        user_id: str,
        ticket_number: str,
        route_id: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        type: Optional[FeedbackType] = None,
        severity: Optional[SeverityType] = None,
        description: Optional[str] = None,
        location: Optional[dict] = None,
        attachments: Optional[list] = None,
        status: Status = Status.RECEIVED,
        created_at: Optional[datetime] = None,
    ) -> Feedback:
        """
        Create a new feedback record in the database.

        Args:
            db: Database session
            feedback_id: UUID for the feedback
            user_id: User ID string (VARCHAR)
            ticket_number: Unique ticket number string for the feedback
            route_id: Optional route ID string
            lat: Optional latitude coordinate
            lon: Optional longitude coordinate
            type: Optional feedback type enum
            severity: Optional severity level enum
            description: Optional feedback description
            location: Optional location dict (JSON)
            attachments: Optional list of attachment URLs (JSON)
            status: Feedback status (default: RECEIVED)
            created_at: Optional creation timestamp (defaults to now)

        Returns:
            Feedback: Created feedback record

        Raises:
            IntegrityError: If ticket_number already exists or other DB constraint violation
        """
        if created_at is None:
            created_at = datetime.utcnow()

        # Convert enum values to strings for storage
        type_value = type.value if type else None
        severity_value = severity.value if severity else None
        status_value = status.value

        # Convert location Pydantic model to dict if needed
        if location is not None and not isinstance(location, dict):
            location = location.dict() if hasattr(location, "dict") else location

        # Convert attachments (HttpUrl objects) to strings if needed
        if attachments is not None:
            attachments = [str(att) for att in attachments]

        feedback = Feedback(
            feedback_id=feedback_id,
            user_id=user_id,
            ticket_number=ticket_number,
            route_id=route_id,
            lat=lat,
            lon=lon,
            type=type_value,
            severity=severity_value,
            description=description,
            location=location,
            attachments=attachments,
            status=status_value,
            created_at=created_at,
            updated_at=created_at,
        )

        db.add(feedback)
        await db.flush()

        return feedback

    async def get_feedback_by_id(
        self,
        db: AsyncSession,
        feedback_id: uuid.UUID,
    ) -> Optional[Feedback]:
        """
        Retrieve a feedback record by ID.

        Args:
            db: Database session
            feedback_id: UUID of the feedback to retrieve

        Returns:
            Feedback record if found, None otherwise
        """
        result = await db.execute(select(Feedback).where(Feedback.feedback_id == feedback_id))
        return result.scalar_one_or_none()

    async def get_feedback_by_ticket(
        self,
        db: AsyncSession,
        ticket_number: str,
    ) -> Optional[Feedback]:
        """
        Retrieve a feedback record by ticket number.

        Args:
            db: Database session
            ticket_number: Ticket number of the feedback to retrieve

        Returns:
            Feedback record if found, None otherwise
        """
        result = await db.execute(select(Feedback).where(Feedback.ticket_number == ticket_number))
        return result.scalar_one_or_none()


# Global factory instance (similar to DatabaseFactory pattern)
_feedback_factory: Optional[FeedbackFactory] = None


def get_feedback_factory() -> FeedbackFactory:
    """
    Get or create the global feedback factory instance.

    Follows the same pattern as get_database_factory() in libs/db.py.

    Returns:
        FeedbackFactory instance
    """
    global _feedback_factory
    if _feedback_factory is None:
        _feedback_factory = FeedbackFactory()
    return _feedback_factory
