from datetime import datetime
from typing import Dict, Literal, Optional

from services.notification.notification_types import NotificationChannel, NotificationType
from pydantic import BaseModel

from services.notification.notification_types import (
    NotificationChannel,
    NotificationType,
)


class Location(BaseModel):
    lat: float
    lon: float
    accuracy_m: Optional[float] = None


class SOSContact(BaseModel):
    name: str
    phone: str


class SOSNotificationRequest(BaseModel):
    sos_id: str
    user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    call_number: Optional[str] = None
    message_template: Optional[str] = None
    variables: Dict[str, str]
    channels: Optional[list[NotificationChannel]] = None
    notification_type: Optional[NotificationType] = NotificationType.SOS
    locale: Optional[str] = "en"


class CreateResp(BaseModel):
    notification_id: str
    status: Literal["queued", "sending", "delivered", "failed", "partial"]


class StatusResult(BaseModel):
    sms_status: Literal["queued", "sending", "sent", "delivered", "failed", "not_triggered"]
    push_status: Literal["sent", "failed", "not_triggered"]
    call_status: Literal["queued", "calling", "answered", "failed", "not_triggered"]


class StatusResp(BaseModel):
    notification_id: str
    sos_id: str
    status: Literal["queued", "sending", "delivered", "failed", "partial"]
    results: StatusResult
    created_at: datetime
    updated_at: datetime


class TestSMSRequest(BaseModel):
    to_phone: str
    message: str


class TestSMSResponse(BaseModel):
    status: Literal["sent", "failed"]
    sid: Optional[str] = None
    to: str
    message: str
    error: Optional[str] = None


class EmergencyCallPoint(BaseModel):
    lat: float
    lon: float


class EmergencyCallRequest(BaseModel):
    sos_id: str
    phone_number: str
    user_location: EmergencyCallPoint
    call_reason: str


class EmergencyCallResponse(BaseModel):
    status: Literal["initiated", "failed"]
    call_id: str
    timestamp: datetime


class EmergencySMSRequest(BaseModel):
    sos_id: str
    user_id: str
    location: Optional[Location] = None
    emergency_contact: SOSContact
    message_template: Optional[str] = None
    variables: Dict[str, str]
    notification_type: Optional[NotificationType] = NotificationType.SOS
    locale: Optional[str] = "en"


class EmergencySMSResponse(BaseModel):
    status: Literal["sent", "failed"]
    sms_id: str
    timestamp: datetime
    message_sent: str
    recipient: str


# Response types for factory senders (based on Swagger API definitions)
class PushNotificationResponse(BaseModel):
    """Response type for push notification sender"""

    status: Literal["sent", "failed"]
    push_id: str
    platform: str


class SMSNotificationResponse(BaseModel):
    """Response type for SMS notification sender (matches Twilio response)"""

    status: Literal["sent", "failed"]
    sid: Optional[str]
    to: str
    from_: Optional[str] = None
    message_status: Optional[str] = None
    error: Optional[str] = None

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {"properties": {"from": {"$ref": "#/properties/from_"}}},
    }


class CallNotificationResponse(BaseModel):
    """Response type for call notification sender"""

    status: Literal["initiated", "failed", "not_triggered", "answered"]
    sid: Optional[str] = None
    error: Optional[str] = None
