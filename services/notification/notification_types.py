from enum import Enum


class NotificationType(str, Enum):
    SOS = "sos"
    LOCATION_SHARE = "location_share"
    RISK_ZONE = "risk_zone"
    GARDA_ALERT = "garda_alert"


class NotificationChannel(str, Enum):
    PUSH = "push"
    SMS = "sms"
    CALL = "call"
