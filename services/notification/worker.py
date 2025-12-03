"""
RabbitMQ Consumer Worker for processing notification messages
Consumes messages from RabbitMQ queue and sends them via Twilio through SOS service
"""

import logging
import os
import sys
import time
from typing import Any, Dict

import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from libs.rabbitmq_client import get_rabbitmq_client
from libs.service_urls import SOS_SERVICE_URL

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def process_sms_notification(message: Dict[str, Any]) -> bool:
    """
    Process an SMS notification message from the queue.
    Sends the SMS via SOS service (which uses Twilio).

    Args:
        message: Message dictionary containing notification details

    Returns:
        True if SMS was sent successfully, False otherwise
    """
    try:
        logger.info(f"Processing SMS notification for SOS ID: {message.get('sos_id')}")
        logger.debug(f"SOS Service URL: {SOS_SERVICE_URL}")

        # Prepare payload for SOS service
        payload = {
            "sos_id": message.get("sos_id"),
            "user_id": message.get("user_id"),
            "location": message.get("location"),
            "emergency_contact": message.get("emergency_contact"),
            "message_template": message.get("message_template"),
            "variables": message.get("variables", {}),
        }

        # Send SMS via SOS service
        # Note: We use httpx in sync mode here since pika uses blocking connections
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{SOS_SERVICE_URL}/v1/emergency/sms", json=payload)
            response.raise_for_status()
            result = response.json()

            if result.get("status") == "sent":
                logger.info(
                    f"✓ SMS sent successfully for SOS ID: {message.get('sos_id')} "
                    f"(SMS ID: {result.get('sms_id')})"
                )
                return True
            else:
                logger.error(
                    f"✗ SMS send failed for SOS ID: {message.get('sos_id')}: "
                    f"{result.get('status')}"
                )
                return False

    except httpx.ConnectError as e:
        logger.error(
            f"✗ Connection refused to SOS service at {SOS_SERVICE_URL} for SOS ID {message.get('sos_id')}: {e}\n"
            f"  Make sure SOS service is running and accessible."
        )
        return False
    except httpx.HTTPError as e:
        logger.error(
            f"✗ HTTP error sending SMS for SOS ID {message.get('sos_id')}: {e}"
        )
        return False
    except Exception as e:
        logger.error(
            f"✗ Unexpected error processing SMS for SOS ID {message.get('sos_id')}: {e}"
        )
        return False


def message_handler(
    message_dict: Dict[str, Any],
    channel,
    method,
    properties,
):
    """
    Handle incoming messages from RabbitMQ queue.

    Args:
        message_dict: Decoded message dictionary
        channel: RabbitMQ channel
        method: Delivery method
        properties: Message properties
    """
    try:
        message_type = message_dict.get("type", "unknown")
        logger.info(f"Received message of type: {message_type}")

        # Track retry count from message headers
        retry_count = 0
        if properties.headers:
            retry_count = properties.headers.get("x-retry-count", 0)

        max_retries = int(os.getenv("RABBITMQ_MAX_MESSAGE_RETRIES", "5"))

        success = False

        if message_type == "sms":
            success = process_sms_notification(message_dict)
        else:
            logger.warning(f"Unknown message type: {message_type}")
            # Acknowledge unknown message types to avoid infinite requeue
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        if success:
            # Acknowledge message on success
            channel.basic_ack(delivery_tag=method.delivery_tag)
            logger.info("Message processed and acknowledged")
        else:
            # Check retry limit
            if retry_count >= max_retries:
                logger.error(
                    f"Message exceeded max retries ({max_retries}). "
                    f"Rejecting without requeue. SOS ID: {message_dict.get('sos_id')}"
                )
                # Reject without requeue - message will be lost or go to dead letter queue
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            else:
                # Calculate delay before retry (exponential backoff)
                delay_seconds = min(60, 2**retry_count)  # Max 60 seconds
                logger.warning(
                    f"Message processing failed (retry {retry_count + 1}/{max_retries}). "
                    f"Will retry after {delay_seconds}s delay. "
                    f"SOS ID: {message_dict.get('sos_id')}"
                )

                # Update retry count in headers
                if not properties.headers:
                    properties.headers = {}
                properties.headers["x-retry-count"] = retry_count + 1

                # Reject and requeue with delay
                # Note: RabbitMQ doesn't support delayed requeue directly,
                # so we'll requeue immediately but log the delay
                # For production, consider using RabbitMQ delayed message plugin
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

                # Sleep to prevent immediate retry loop
                time.sleep(delay_seconds)

    except Exception as e:
        logger.error(f"Error in message handler: {e}")
        # Get retry count
        retry_count = 0
        if properties.headers:
            retry_count = properties.headers.get("x-retry-count", 0)

        max_retries = int(os.getenv("RABBITMQ_MAX_MESSAGE_RETRIES", "5"))

        if retry_count >= max_retries:
            logger.error("Exception exceeded max retries. Rejecting without requeue.")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        else:
            delay_seconds = min(60, 2**retry_count)
            logger.warning(
                f"Exception occurred. Will retry after {delay_seconds}s delay."
            )
            if not properties.headers:
                properties.headers = {}
            properties.headers["x-retry-count"] = retry_count + 1
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            time.sleep(delay_seconds)


def main():
    """Main function to start the RabbitMQ consumer"""
    logger.info("Starting RabbitMQ notification worker...")

    queue_name = os.getenv("RABBITMQ_NOTIFICATION_QUEUE", "notifications")
    max_retries = int(os.getenv("RABBITMQ_MAX_RETRIES", "10"))
    retry_delay = int(os.getenv("RABBITMQ_RETRY_DELAY", "5"))  # seconds

    rabbitmq = None

    # Retry connection with exponential backoff
    for attempt in range(max_retries):
        try:
            rabbitmq = get_rabbitmq_client()

            # Connect to RabbitMQ
            if rabbitmq.connect():
                logger.info(
                    f"✓ Connected to RabbitMQ. Consuming from queue: '{queue_name}'"
                )
                break
            else:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2**attempt)  # Exponential backoff
                    logger.warning(
                        f"Connection attempt {attempt + 1}/{max_retries} failed. "
                        f"Retrying in {wait_time} seconds..."
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(
                        f"Failed to connect to RabbitMQ after {max_retries} attempts. "
                        f"Please check your configuration:\n"
                        f"  - RABBITMQ_HOST={os.getenv('RABBITMQ_HOST', 'not set')}\n"
                        f"  - RABBITMQ_PORT={os.getenv('RABBITMQ_PORT', 'not set')}\n"
                        f"  - RABBITMQ_USE_SSL={os.getenv('RABBITMQ_USE_SSL', 'not set')}\n"
                        f"  - Check firewall/network connectivity\n"
                        f"  - Verify RabbitMQ is running and accessible"
                    )
                    sys.exit(1)
        except KeyboardInterrupt:
            logger.info("Worker stopped by user")
            return
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2**attempt)
                logger.warning(
                    f"Connection error (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {wait_time} seconds..."
                )
                time.sleep(wait_time)
            else:
                logger.error(f"Worker error after {max_retries} attempts: {e}")
                sys.exit(1)

    # Start consuming messages
    try:
        rabbitmq.consume(
            queue_name=queue_name,
            callback=message_handler,
            durable=True,
            prefetch_count=1,  # Process one message at a time
        )
    except KeyboardInterrupt:
        logger.info("Worker stopped by user")
    except Exception as e:
        logger.error(f"Error consuming messages: {e}")
        sys.exit(1)
    finally:
        try:
            if rabbitmq:
                rabbitmq.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
