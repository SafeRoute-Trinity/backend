"""
Notification Status Enums
Shared status enumerations for notification and SOS services.
"""

from enum import Enum


class SMSStatus(str, Enum):
    """SMS notification status values"""
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    NOT_TRIGGERED = "not_triggered"


class PushStatus(str, Enum):
    """Push notification status values"""
    SENT = "sent"
    FAILED = "failed"
    NOT_TRIGGERED = "not_triggered"


class CallStatus(str, Enum):
    """Call notification status values"""
    QUEUED = "queued"
    CALLING = "calling"
    ANSWERED = "answered"
    FAILED = "failed"
    NOT_TRIGGERED = "not_triggered"
    INITIATED = "initiated"


class NotificationStatus(str, Enum):
    """Overall notification status values"""
    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    PARTIAL = "partial"


class EmergencyStatus(str, Enum):
    """Emergency service status values (used by SOS service)"""
    SENT = "sent"
    FAILED = "failed"
    INITIATED = "initiated"
