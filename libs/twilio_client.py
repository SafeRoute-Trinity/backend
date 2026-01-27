"""
Twilio SMS/Voice Client
Sends SMS and makes voice calls using Twilio API
"""

import os
from typing import Optional

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client


class TwilioClient:
    """Wrapper for Twilio SMS and Voice services"""

    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_phone = os.getenv("TWILIO_PHONE_NUMBER")

        if not all([self.account_sid, self.auth_token, self.from_phone]):
            raise ValueError(
                "Missing Twilio configuration. Please set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER in your .env file"
            )

        self.client = Client(self.account_sid, self.auth_token)

    def send_sms(self, to_phone: str, message: str, from_phone: Optional[str] = None) -> dict:
        """
        Send an SMS message

        Args:
            to_phone: Recipient phone number (E.164 format, e.g., +1234567890)
            message: Message content
            from_phone: Optional sender phone number (defaults to configured number)

        Returns:
            dict with status, sid, and any error information
        """
        try:
            msg = self.client.messages.create(
                body=message, from_=from_phone or self.from_phone, to=to_phone
            )

            return {
                "status": "sent",
                "sid": msg.sid,
                "to": msg.to,
                "from": msg.from_,
                "message_status": msg.status,
                "error": None,
            }
        except TwilioRestException as e:
            return {
                "status": "failed",
                "sid": None,
                "to": to_phone,
                "from": from_phone or self.from_phone,
                "message_status": "failed",
                "error": str(e),
            }
        except Exception as e:
            return {
                "status": "failed",
                "sid": None,
                "to": to_phone,
                "from": from_phone or self.from_phone,
                "message_status": "failed",
                "error": f"Unexpected error: {str(e)}",
            }

    def make_call(self, to_phone: str, twiml_url: str, from_phone: Optional[str] = None) -> dict:
        """
        Make a voice call

        Args:
            to_phone: Recipient phone number (E.164 format)
            twiml_url: URL that returns TwiML instructions for the call
            from_phone: Optional caller phone number (defaults to configured number)

        Returns:
            dict with status, sid, and any error information
        """
        try:
            call = self.client.calls.create(
                url=twiml_url, to=to_phone, from_=from_phone or self.from_phone
            )

            return {
                "status": "initiated",
                "sid": call.sid,
                "to": call.to,
                "from": call.from_,
                "call_status": call.status,
                "error": None,
            }
        except TwilioRestException as e:
            return {
                "status": "failed",
                "sid": None,
                "to": to_phone,
                "from": from_phone or self.from_phone,
                "call_status": "failed",
                "error": str(e),
            }
        except Exception as e:
            return {
                "status": "failed",
                "sid": None,
                "to": to_phone,
                "from": from_phone or self.from_phone,
                "call_status": "failed",
                "error": f"Unexpected error: {str(e)}",
            }

    def get_message_status(self, message_sid: str) -> dict:
        """
        Check the status of a sent message

        Args:
            message_sid: The Twilio message SID

        Returns:
            dict with message status information
        """
        try:
            msg = self.client.messages(message_sid).fetch()
            return {
                "status": "success",
                "sid": msg.sid,
                "message_status": msg.status,
                "error_code": msg.error_code,
                "error_message": msg.error_message,
                "to": msg.to,
                "from": msg.from_,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def get_call_status(self, call_sid: str) -> dict:
        """
        Check the status of a call

        Args:
            call_sid: The Twilio call SID

        Returns:
            dict with call status information
        """
        try:
            call = self.client.calls(call_sid).fetch()
            return {
                "status": "success",
                "sid": call.sid,
                "call_status": call.status,
                "duration": call.duration,
                "to": call.to,
                "from": call.from_,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}


# Singleton instance
_twilio_client: Optional[TwilioClient] = None


def get_twilio_client() -> TwilioClient:
    """Get or create the Twilio client singleton"""
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = TwilioClient()
    return _twilio_client
