import uuid
from datetime import datetime
from typing import Dict, Iterable, Literal

from fastapi import HTTPException

from services.notification.factory import NotificationFactory
from services.notification.models import (
    CreateResp,
    EmergencyCallRequest,
    EmergencyCallResponse,
    EmergencySMSRequest,
    EmergencySMSResponse,
    SOSNotificationRequest,
    StatusResp,
    StatusResult,
)
from services.notification.templates import get_template

DEFAULT_CHANNELS: list[Literal["push", "sms", "call"]] = ["push", "sms"]
SUCCESS_STATUSES = {"sent", "delivered", "answered", "connected", "initiated"}
FAIL_STATUSES = {"failed", "not_triggered"}


class NotificationManager:
    def __init__(self, store: Dict[str, dict]) -> None:
        self._store = store
        self._factory = NotificationFactory()

    def _render_message(self, template: str, variables: Dict[str, str]) -> str:
        message = template
        for key, value in variables.items():
            message = message.replace(f"{{{key}}}", value)
        return message

    def _append_location(self, message: str, location) -> str:
        if not location:
            return message
        location_text = (
            f"\n\nLocation: https://maps.google.com/?q={location.lat},{location.lon}"
        )
        if location.accuracy_m:
            location_text += f" (±{location.accuracy_m}m)"
        return message + location_text

    def _aggregate_status(self, statuses: Iterable[str]) -> str:
        status_list = list(statuses)
        successes = [s for s in status_list if s in SUCCESS_STATUSES]
        failures = [s for s in status_list if s in FAIL_STATUSES]
        if successes and not failures:
            return "delivered"
        if successes and failures:
            return "partial"
        if failures and not successes:
            return "failed"
        return "failed"

    async def send_sos_notification(self, body: SOSNotificationRequest) -> CreateResp:
        notification_id = f"ntf_{uuid.uuid4().hex[:6]}"
        now = datetime.utcnow()

        channels = body.channels or list(DEFAULT_CHANNELS)
        results = {
            "sms_status": "not_triggered",
            "push_status": "not_triggered",
            "call_status": "not_triggered",
        }

        template = body.message_template
        notification_type = (
            body.notification_type.value
            if body.notification_type is not None
            else "sos"
        )
        if not template and notification_type:
            template = get_template(notification_type, "sms", body.locale or "en")
        if not template:
            raise HTTPException(status_code=400, detail="Missing message template")
        message = self._render_message(template, body.variables)
        message = self._append_location(message, body.location)

        if "push" in channels:
            try:
                sender = self._factory.get_sender("push")
                push_template = get_template(
                    notification_type, "push", body.locale or "en"
                )
                push_message = (
                    self._render_message(push_template, body.variables)
                    if push_template
                    else message
                )
                push_result = await sender.send(
                    {
                        "user_id": body.user_id,
                        "message": push_message,
                        "location": body.location.model_dump()
                        if body.location
                        else None,
                    }
                )
                results["push_status"] = (
                    "sent" if push_result.get("status") == "sent" else "failed"
                )
            except Exception:
                results["push_status"] = "failed"

        if "sms" in channels:
            try:
                sender = self._factory.get_sender("sms")
                sms_result = await sender.send(
                    {
                        "to_phone": body.emergency_contact.phone,
                        "message": message,
                    }
                )
                results["sms_status"] = (
                    "sent" if sms_result.get("status") == "sent" else "failed"
                )
            except Exception:
                results["sms_status"] = "failed"

        if "call" in channels:
            try:
                sender = self._factory.get_sender("call")
                call_result = await sender.send(
                    {
                        "to_phone": body.call_number,
                        "sos_id": body.sos_id,
                    }
                )
                results["call_status"] = (
                    "answered"
                    if call_result.get("status") == "answered"
                    else "failed"
                )
            except Exception:
                results["call_status"] = "failed"

        notification_status = self._aggregate_status(
            [
                results["push_status"] if "push" in channels else "not_triggered",
                results["sms_status"] if "sms" in channels else "not_triggered",
                results["call_status"] if "call" in channels else "not_triggered",
            ]
        )

        self._store[notification_id] = {
            "notification_id": notification_id,
            "sos_id": body.sos_id,
            "status": notification_status,
            "results": results,
            "created_at": now,
            "updated_at": now,
        }

        return CreateResp(notification_id=notification_id, status=notification_status)

    async def send_emergency_sms(
        self, body: EmergencySMSRequest
    ) -> EmergencySMSResponse:
        template = body.message_template
        notification_type = (
            body.notification_type.value
            if body.notification_type is not None
            else "sos"
        )
        if not template and notification_type:
            template = get_template(notification_type, "sms", body.locale or "en")
        if not template:
            raise HTTPException(status_code=400, detail="Missing message template")
        message = self._render_message(template, body.variables)
        message = self._append_location(message, body.location)

        try:
            sender = self._factory.get_sender("sms")
            result = await sender.send(
                {
                    "to_phone": body.emergency_contact.phone,
                    "message": message,
                }
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to send SMS: {str(e)}")

        status = "sent" if result.get("status") == "sent" else "failed"
        sms_id = result.get("sid") or f"SMS-{uuid.uuid4().hex[:6]}"

        return EmergencySMSResponse(
            status=status,
            sms_id=sms_id,
            timestamp=datetime.utcnow(),
            message_sent=message,
            recipient=body.emergency_contact.phone,
        )

    async def send_emergency_call(
        self, body: EmergencyCallRequest
    ) -> EmergencyCallResponse:
        call_id = f"CALL-{uuid.uuid4().hex[:6]}"
        # Call delivery is not implemented yet; keep current behavior.
        return EmergencyCallResponse(
            status="initiated", call_id=call_id, timestamp=datetime.utcnow()
        )

    def get_status(self, notification_id: str) -> StatusResp:
        now = datetime.utcnow()
        ntf = self._store.get(notification_id)
        if not ntf:
            ntf = {
                "notification_id": notification_id,
                "sos_id": "SOS-demo",
                "status": "delivered",
                "results": {
                    "sms_status": "delivered",
                    "push_status": "sent",
                    "call_status": "not_triggered",
                },
                "created_at": now,
                "updated_at": now,
            }
        return StatusResp(**ntf)
