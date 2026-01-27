"""
Configuration module for loading environment variables.

Provides centralized configuration management for the SafeRoute backend.
"""

import os


class Config:
    """
    Application configuration class.

    Loads configuration from environment variables. All configuration values
    are class attributes that can be accessed without instantiating the class.

    Attributes:
        TWILIO_ACCOUNT_SID: Twilio account SID from environment
        TWILIO_AUTH_TOKEN: Twilio authentication token from environment
        TWILIO_PHONE_NUMBER: Twilio phone number from environment
    """

    # Twilio Configuration
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

    @classmethod
    def validate_twilio_config(cls):
        """
        Check if Twilio configuration is complete.

        Returns:
            True if all Twilio configuration values are set, False otherwise
        """
        return all(
            [
                cls.TWILIO_ACCOUNT_SID,
                cls.TWILIO_AUTH_TOKEN,
                cls.TWILIO_PHONE_NUMBER,
            ]
        )


# Global configuration instance
config = Config()
