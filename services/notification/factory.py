import os
from typing import Dict, Union

from twilio.twiml.voice_response import VoiceResponse

from libs.twilio_client import get_twilio_client
from services.notification.schemas import (
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
        twilio_result = twilio.send_sms(to_phone=payload["to_phone"], message=payload["message"])
        return SMSNotificationResponse(
            status=twilio_result["status"],
            sid=twilio_result.get("sid"),
            to=twilio_result.get("to", payload["to_phone"]),
            from_=twilio_result.get("from"),
            message_status=twilio_result.get("message_status"),
            error=twilio_result.get("error"),
        )


class CallSender(BaseSender):
    """Call notification sender — uses Twilio Voice with inline TwiML."""

    async def send(self, payload: Dict) -> CallNotificationResponse:
        """
        Place a voice call via Twilio using inline TwiML.

        Args:
            payload: Dictionary with:
                - to_phone: E.164 phone number to call
                - call_reason: text to speak when the call is answered
                - locale: BCP-47 language tag (default "en")

        Returns:
            CallNotificationResponse
        """
        to_phone = payload.get("to_phone", "")
        call_reason = payload.get("call_reason", "Emergency SOS alert")
        locale = payload.get("locale", "en")

        if not to_phone:
            return CallNotificationResponse(
                status="failed",
                sid=None,
                error="missing_phone_number",
            )

        mode = os.getenv("NOTIFICATION_CALL_MODE", "").lower()
        if mode in {"dummy", "dev", "test"}:
            return CallNotificationResponse(
                status="initiated",
                sid="CALL-DUMMY",
                error=None,
            )

        # Build TwiML: repeat the emergency message twice so the recipient
        # has time to process it.
        resp = VoiceResponse()
        resp.say(call_reason, voice="alice", language=locale, loop=2)
        resp.pause(length=1)
        resp.say(
            "This is an automated SafeRoute emergency call. "
            "Please check your SMS for location details.",
            voice="alice",
            language="en",
        )
        twiml_str = str(resp)

        twilio = get_twilio_client()
        result = twilio.make_call_twiml(to_phone=to_phone, twiml=twiml_str)

        return CallNotificationResponse(
            status=result["status"],
            sid=result.get("sid"),
            error=result.get("error"),
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
