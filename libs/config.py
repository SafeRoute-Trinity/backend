"""
Configuration module for loading environment variables
"""

import os
from typing import Optional


class Config:
    """Application configuration"""

    # Twilio Configuration
    TWILIO_ACCOUNT_SID: Optional[str] = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN: Optional[str] = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER: Optional[str] = os.getenv("TWILIO_PHONE_NUMBER")

    @classmethod
    def validate_twilio_config(cls) -> bool:
        """Check if Twilio configuration is complete"""
        return all(
            [cls.TWILIO_ACCOUNT_SID, cls.TWILIO_AUTH_TOKEN, cls.TWILIO_PHONE_NUMBER]
        )


config = Config()
