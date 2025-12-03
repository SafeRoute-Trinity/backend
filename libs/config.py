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

    # RabbitMQ Configuration
    RABBITMQ_HOST: Optional[str] = os.getenv("RABBITMQ_HOST", "localhost")
    RABBITMQ_PORT: Optional[str] = os.getenv(
        "RABBITMQ_PORT"
    )  # None = auto-detect based on SSL
    RABBITMQ_USERNAME: Optional[str] = os.getenv("RABBITMQ_USERNAME", "guest")
    RABBITMQ_PASSWORD: Optional[str] = os.getenv("RABBITMQ_PASSWORD", "guest")
    RABBITMQ_USE_SSL: Optional[str] = os.getenv("RABBITMQ_USE_SSL", "false")
    RABBITMQ_CONNECTION_TIMEOUT: int = int(
        os.getenv("RABBITMQ_CONNECTION_TIMEOUT", "30")
    )
    RABBITMQ_NOTIFICATION_QUEUE: str = os.getenv(
        "RABBITMQ_NOTIFICATION_QUEUE", "notifications"
    )

    @classmethod
    def validate_twilio_config(cls) -> bool:
        """Check if Twilio configuration is complete"""
        return all(
            [cls.TWILIO_ACCOUNT_SID, cls.TWILIO_AUTH_TOKEN, cls.TWILIO_PHONE_NUMBER]
        )


config = Config()
