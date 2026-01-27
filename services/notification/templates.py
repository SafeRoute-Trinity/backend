from typing import Dict


# In-code templates for now; later swap this module with DB-backed store.
# Key format: "{notification_type}.{channel}.{locale}"
TEMPLATES: Dict[str, str] = {
    "sos.sms.en": "Emergency for {name}",
    "sos.push.en": "SOS alert for {name}",
    "location_share.sms.en": "{name} shared their location: {link}",
    "risk_zone.push.en": "Warning: you entered a high risk zone near {area}",
    "garda_alert.sms.en": "SOS for {name}. Contact: {phone}. Location: {link}",
}


def get_template(notification_type: str, channel: str, locale: str) -> str:
    key = f"{notification_type}.{channel}.{locale}"
    return TEMPLATES.get(key, "")
