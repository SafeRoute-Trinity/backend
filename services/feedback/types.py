"""
Type definitions for feedback service.

This module contains all enum types used in the feedback service.
"""

from enum import Enum


class Status(str, Enum):
    """Feedback status enumeration."""

    RECEIVED = "received"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class SeverityType(str, Enum):
    """Feedback severity level enumeration."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FeedbackType(str, Enum):
    """Feedback type/category enumeration."""

    SAFETY_ISSUE = "safety_issue"
    ROUTE_QUALITY = "route_quality"
    OTHERS = "others"
