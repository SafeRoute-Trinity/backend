import uuid
from datetime import datetime
from typing import Dict, Iterable, Literal

from fastapi import HTTPException

from common.notification_status import (
    CallStatus,
    EmergencyStatus,
    NotificationStatus,
    PushStatus,
    SMSStatus,
)
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
SUCCESS_STATUSES = {
    SMSStatus.SENT.value,
    SMSStatus.DELIVERED.value,
    CallStatus.ANSWERED.value,
    PushStatus.SENT.value,
    CallStatus.INITIATED.value,
}
FAIL_STATUSES = {
    SMSStatus.FAILED.value,
    CallStatus.FAILED.value,
    PushStatus.FAILED.value,
    CallStatus.NOT_TRIGGERED.value,
}


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
        location_text = f"\n\nLocation: https://maps.google.com/?q={location.lat},{location.lon}"
        if location.accuracy_m:
            location_text += f" (±{location.accuracy_m}m)"
        return message + location_text

    def _aggregate_status(self, statuses: Iterable[str]) -> str:
        status_list = list(statuses)
        successes = [s for s in status_list if s in SUCCESS_STATUSES]
        failures = [s for s in status_list if s in FAIL_STATUSES]
        if successes and not failures:
            return NotificationStatus.DELIVERED.value
        if successes and failures:
            return NotificationStatus.PARTIAL.value
        if failures and not successes:
            return NotificationStatus.FAILED.value
        return NotificationStatus.FAILED.value

    async def send_sos_notification(self, body: SOSNotificationRequest) -> CreateResp:
        notification_id = f"ntf_{uuid.uuid4().hex[:6]}"
        now = datetime.utcnow()

        raw_channels = body.channels or list(DEFAULT_CHANNELS)
        channels = [c.value if hasattr(c, "value") else str(c) for c in raw_channels]
        results = {
            "sms_status": SMSStatus.NOT_TRIGGERED.value,
            "push_status": PushStatus.NOT_TRIGGERED.value,
            "call_status": CallStatus.NOT_TRIGGERED.value,
        }

        template = body.message_template
        notification_type = (
            getattr(body.notification_type, "value", body.notification_type)
            if body.notification_type is not None
            else "sos"
        )
        if not isinstance(notification_type, str):
            notification_type = "sos"
        if not template and notification_type:
            template = get_template(notification_type, "sms", body.locale or "en")
        if not template:
            raise HTTPException(status_code=400, detail="Missing message template")
        message = self._render_message(template, body.variables)
        message = self._append_location(message, body.location)

        if "push" in channels:
            try:
                sender = self._factory.get_sender("push")
                push_template = get_template(notification_type, "push", body.locale or "en")
                push_message = (
                    self._render_message(push_template, body.variables)
                    if push_template
                    else message
                )
                location_dict = None
                if body.location:
                    location_dict = (
                        body.location.model_dump()
                        if hasattr(body.location, "model_dump")
                        else body.location.dict()
                    )
                push_result = await sender.send(
                    {
                        "user_id": body.user_id,
                        "message": push_message,
                        "location": location_dict,
                    }
                )
                results["push_status"] = (
                    PushStatus.SENT.value
                    if push_result.status == "sent"
                    else PushStatus.FAILED.value
                )
            except Exception:
                results["push_status"] = PushStatus.FAILED.value

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
                    SMSStatus.SENT.value if sms_result.status == "sent" else SMSStatus.FAILED.value
                )
            except Exception:
                results["sms_status"] = SMSStatus.FAILED.value

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
                    CallStatus.ANSWERED.value
                    if call_result.status == "answered"
                    else CallStatus.FAILED.value
                )
            except Exception:
                results["call_status"] = CallStatus.FAILED.value

        notification_status = self._aggregate_status(
            [
                results["push_status"] if "push" in channels else PushStatus.NOT_TRIGGERED.value,
                results["sms_status"] if "sms" in channels else SMSStatus.NOT_TRIGGERED.value,
                results["call_status"] if "call" in channels else CallStatus.NOT_TRIGGERED.value,
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

    async def send_emergency_sms(self, body: EmergencySMSRequest) -> EmergencySMSResponse:
        template = body.message_template
        notification_type = (
            getattr(body.notification_type, "value", body.notification_type)
            if body.notification_type is not None
            else "sos"
        )
        if not isinstance(notification_type, str):
            notification_type = "sos"
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

        status = (
            EmergencyStatus.SENT.value if result.status == "sent" else EmergencyStatus.FAILED.value
        )
        sms_id = result.sid or f"SMS-{uuid.uuid4().hex[:6]}"

        return EmergencySMSResponse(
            status=status,
            sms_id=sms_id,
            timestamp=datetime.utcnow(),
            message_sent=message,
            recipient=body.emergency_contact.phone,
        )

    async def send_emergency_call(self, body: EmergencyCallRequest) -> EmergencyCallResponse:
        call_id = f"CALL-{uuid.uuid4().hex[:6]}"
        # Call delivery is not implemented yet; keep current behavior.
        return EmergencyCallResponse(
            status=EmergencyStatus.INITIATED.value, call_id=call_id, timestamp=datetime.utcnow()
        )

    def get_status(self, notification_id: str) -> StatusResp:
        now = datetime.utcnow()
        ntf = self._store.get(notification_id)
        if not ntf:
            ntf = StatusResp(
                notification_id=notification_id,
                sos_id="SOS-demo",
                status=NotificationStatus.DELIVERED.value,
                results=StatusResult(
                    sms_status=SMSStatus.DELIVERED.value,
                    push_status=PushStatus.SENT.value,
                    call_status=CallStatus.NOT_TRIGGERED.value,
                ),
                created_at=now,
                updated_at=now,
            )
        else:
            # Ensure the stored data is properly typed as StatusResp
            ntf = StatusResp(**ntf)
        return ntf
