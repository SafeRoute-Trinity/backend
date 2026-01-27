import os
from typing import Any, Dict

from libs.twilio_client import get_twilio_client


class BaseSender:
    async def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Sender must implement send()")


class PushSender(BaseSender):
    async def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Dummy push sender until a real provider is configured.
        return {
            "status": "sent",
            "push_id": payload.get("push_id", "push_dummy"),
            "platform": "dummy",
        }


class SmsSender(BaseSender):
    async def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        mode = os.getenv("NOTIFICATION_SMS_MODE", "").lower()
        if mode in {"dummy", "dev", "test"}:
            return {
                "status": "sent",
                "sid": "SMS-DUMMY",
                "to": payload.get("to_phone"),
                "from": "dummy",
                "message_status": "sent",
                "error": None,
            }
        twilio = get_twilio_client()
        return twilio.send_sms(to_phone=payload["to_phone"], message=payload["message"])


class CallSender(BaseSender):
    async def send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Placeholder until call flow (TwiML URL) is defined.
        return {"status": "not_triggered", "sid": None, "error": "call_not_configured"}


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
