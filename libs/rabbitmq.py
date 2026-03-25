"""
Async RabbitMQ client for SafeRoute backend.

Implements a simple publish/consume pattern using aio-pika:

  - Publisher: connects on first use, publishes JSON messages to a queue.
  - Consumer:  connects on startup, listens to a queue and calls a handler
               for each incoming message.

Graceful degradation:
  If RabbitMQ is unavailable, publish() logs a warning and returns False
  instead of crashing the service.  The caller decides how to handle it
  (e.g. fall back to direct HTTP).

Queue durability:
  All queues are declared as durable=True so messages survive a RabbitMQ
  restart.  Messages are published as persistent (delivery_mode=2).
"""

import json
import logging
from typing import Any, Callable, Coroutine, Optional

import aio_pika
from aio_pika import DeliveryMode, Message

import os

from common.constants import (
    QUEUE_FEEDBACK_EMAIL,
    QUEUE_FEEDBACK_SUBMIT,
    QUEUE_SOS_NOTIFICATION,
    RABBITMQ_HOST,
    RABBITMQ_PORT,
    RABBITMQ_USER,
    RABBITMQ_VHOST,
)

logger = logging.getLogger(__name__)

# All known durable queues - declared on connect so they exist before any
# publisher or consumer tries to use them.
_QUEUES = [QUEUE_SOS_NOTIFICATION, QUEUE_FEEDBACK_EMAIL, QUEUE_FEEDBACK_SUBMIT]


class RabbitMQClient:
    """Async RabbitMQ connection wrapper.

    One instance should be shared per service (store on app.state).
    Call connect() on startup and close() on shutdown.
    """

    def __init__(self) -> None:
        self._connection: Optional[aio_pika.RobustConnection] = None
        self._channel: Optional[aio_pika.abc.AbstractChannel] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open connection and declare all durable queues.

        Returns True on success, False if RabbitMQ is unreachable.
        """
        rabbitmq_password = os.getenv("RABBITMQ_PASSWORD", "guest")
        url = (
            f"amqp://{RABBITMQ_USER}:{rabbitmq_password}"
            f"@{RABBITMQ_HOST}:{RABBITMQ_PORT}{RABBITMQ_VHOST}"
        )
        try:
            self._connection = await aio_pika.connect_robust(url)
            self._channel = await self._connection.channel()
            # Declare all queues so they exist before first use
            for queue_name in _QUEUES:
                await self._channel.declare_queue(queue_name, durable=True)
            logger.info("RabbitMQ connected: %s:%s", RABBITMQ_HOST, RABBITMQ_PORT)
            return True
        except Exception as exc:
            logger.warning("RabbitMQ unavailable (%s). Messaging disabled until reconnect.", exc)
            self._connection = None
            self._channel = None
            return False

    async def close(self) -> None:
        """Close the RabbitMQ connection gracefully."""
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
            logger.info("RabbitMQ connection closed.")
        self._connection = None
        self._channel = None

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, queue_name: str, payload: dict[str, Any]) -> bool:
        """Publish a JSON message to *queue_name*.

        Returns True if published, False if RabbitMQ is unavailable.
        """
        if self._channel is None:
            logger.warning(
                "RabbitMQ not connected. Cannot publish to '%s'. Payload: %s",
                queue_name,
                payload,
            )
            return False

        try:
            message = Message(
                body=json.dumps(payload).encode(),
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            )
            await self._channel.default_exchange.publish(message, routing_key=queue_name)
            logger.debug("Published to '%s': %s", queue_name, payload)
            return True
        except Exception as exc:
            logger.error("Failed to publish to '%s': %s", queue_name, exc)
            return False

    # ------------------------------------------------------------------
    # Consuming
    # ------------------------------------------------------------------

    async def consume(
        self,
        queue_name: str,
        handler: Callable[[dict[str, Any]], Coroutine],
    ) -> None:
        """Start consuming messages from *queue_name*.

        *handler* is an async function that receives the decoded JSON payload.
        Messages are auto-acknowledged after a successful handler call.
        If the handler raises, the message is nack'd (requeued once).
        """
        if self._channel is None:
            logger.warning("RabbitMQ not connected. Cannot start consumer for '%s'.", queue_name)
            return

        queue = await self._channel.declare_queue(queue_name, durable=True)

        async def _on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            async with message.process(requeue=True):
                try:
                    payload = json.loads(message.body.decode())
                    await handler(payload)
                except Exception as exc:
                    logger.error("Consumer error on queue '%s': %s", queue_name, exc)
                    raise  # triggers requeue via process(requeue=True)

        await queue.consume(_on_message)
        logger.info("Consumer started for queue '%s'.", queue_name)
