import os
from typing import Dict, Union

from libs.twilio_client import get_twilio_client
from services.notification.models import (
    CallNotificationResponse,
    PushNotificationResponse,
    SMSNotificationResponse,
)


class BaseSender:
    """Base class for notification senders"""
    
    async def send(
        self, payload: Dict
    ) -> Union[PushNotificationResponse, SMSNotificationResponse, CallNotificationResponse]:
        """
        Send notification via this channel.
        
        Args:
            payload: Channel-specific payload dictionary
            
        Returns:
            Channel-specific response model based on Swagger API definition
        """
        raise NotImplementedError("Sender must implement send()")


class PushSender(BaseSender):
    """Push notification sender"""
    
    async def send(self, payload: Dict) -> PushNotificationResponse:
        """
        Send push notification.
        
        Args:
            payload: Dictionary with user_id, message, location
            
        Returns:
            PushNotificationResponse matching Swagger API definition
        """
        # Dummy push sender until a real provider is configured.
        return PushNotificationResponse(
            status="sent",
            push_id=payload.get("push_id", "push_dummy"),
            platform="dummy",
        )


class SmsSender(BaseSender):
    """SMS notification sender"""
    
    async def send(self, payload: Dict) -> SMSNotificationResponse:
        """
        Send SMS notification.
        
        Args:
            payload: Dictionary with to_phone, message
            
        Returns:
            SMSNotificationResponse matching Swagger API definition
        """
        mode = os.getenv("NOTIFICATION_SMS_MODE", "").lower()
        if mode in {"dummy", "dev", "test"}:
            return SMSNotificationResponse(
                status="sent",
                sid="SMS-DUMMY",
                to=payload.get("to_phone", ""),
                from_="dummy",
                message_status="sent",
                error=None,
            )
        twilio = get_twilio_client()
        twilio_result = twilio.send_sms(
            to_phone=payload["to_phone"], message=payload["message"]
        )
        return SMSNotificationResponse(
            status=twilio_result["status"],
            sid=twilio_result.get("sid"),
            to=twilio_result.get("to", payload["to_phone"]),
            from_=twilio_result.get("from"),
            message_status=twilio_result.get("message_status"),
            error=twilio_result.get("error"),
        )


class CallSender(BaseSender):
    """Call notification sender"""
    
    async def send(self, payload: Dict) -> CallNotificationResponse:
        """
        Send call notification.
        
        Args:
            payload: Dictionary with to_phone, sos_id
            
        Returns:
            CallNotificationResponse matching Swagger API definition
        """
        # Placeholder until call flow (TwiML URL) is defined.
        return CallNotificationResponse(
            status="not_triggered",
            sid=None,
            error="call_not_configured",
        )


class NotificationFactory:
    def __init__(self) -> None:
        self._senders = {
            "push": PushSender(),
            "sms": SmsSender(),
            "call": CallSender(),
        }

    def get_sender(self, channel: str) -> BaseSender:
        if channel not in self._senders:
            raise ValueError(f"Unsupported channel: {channel}")
        return self._senders[channel]
