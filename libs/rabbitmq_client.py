"""
RabbitMQ Client for publishing and consuming messages
Handles connection management and message queuing
"""

import json
import logging
import os
import re
import ssl
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import pika
from pika.exceptions import AMQPConnectionError

logger = logging.getLogger(__name__)


def extract_hostname(host_or_url: str) -> str:
    """
    Extract hostname from a URL or return the hostname as-is.

    Args:
        host_or_url: Hostname or URL (e.g., 'localhost' or 'https://rabbitmq.example.com')

    Returns:
        Hostname string
    """
    # If it looks like a URL, parse it
    if "://" in host_or_url:
        parsed = urlparse(host_or_url)
        return parsed.hostname or host_or_url
    # Remove any trailing path or query strings
    host_or_url = re.sub(r"[/?#].*$", "", host_or_url)
    return host_or_url


class RabbitMQClient:
    """RabbitMQ client for publishing and consuming messages"""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        virtual_host: str = "/",
        use_ssl: Optional[bool] = None,
        connection_timeout: int = 10,
    ):
        """
        Initialize RabbitMQ client

        Args:
            host: RabbitMQ host (defaults to RABBITMQ_HOST env var or 'localhost')
            port: RabbitMQ port (defaults to RABBITMQ_PORT env var or 5672/5671 for SSL)
            username: RabbitMQ username (defaults to RABBITMQ_USERNAME env var or 'guest')
            password: RabbitMQ password (defaults to RABBITMQ_PASSWORD env var or 'guest')
            virtual_host: Virtual host (defaults to '/')
            use_ssl: Whether to use SSL/TLS (defaults to RABBITMQ_USE_SSL env var or False)
            connection_timeout: Connection timeout in seconds (defaults to 10)
        """
        # Check if running in Kubernetes (has KUBERNETES_SERVICE_HOST env var)
        # or use explicit host from env
        if host:
            host_env = host
        elif os.getenv("KUBERNETES_SERVICE_HOST"):
            # Running in Kubernetes - use internal service name
            host_env = os.getenv("RABBITMQ_HOST", "rabbitmq.data.svc.cluster.local")
        else:
            # Local development - use configured host or localhost
            host_env = os.getenv("RABBITMQ_HOST", "localhost")

        self.host = extract_hostname(host_env)

        # Determine if SSL should be used
        if use_ssl is None:
            ssl_env = os.getenv("RABBITMQ_USE_SSL", "").lower()
            self.use_ssl = ssl_env in ("true", "1", "yes")
        else:
            self.use_ssl = use_ssl

        # Set default port based on SSL
        if port is None:
            port_env = os.getenv("RABBITMQ_PORT")
            if port_env:
                self.port = int(port_env)
            else:
                self.port = 5671 if self.use_ssl else 5672
        else:
            self.port = port

        self.username = username or os.getenv("RABBITMQ_USERNAME", "guest")
        self.password = password or os.getenv("RABBITMQ_PASSWORD", "guest")
        self.virtual_host = virtual_host
        self.connection_timeout = int(
            os.getenv("RABBITMQ_CONNECTION_TIMEOUT", str(connection_timeout))
        )

        self.connection: Optional[pika.BlockingConnection] = None
        self.channel: Optional[pika.channel.Channel] = None

    def connect(self) -> bool:
        """
        Establish connection to RabbitMQ

        Returns:
            True if connection successful, False otherwise
        """
        try:
            credentials = pika.PlainCredentials(self.username, self.password)

            # Configure SSL if needed
            ssl_options = None
            if self.use_ssl:
                ssl_context = ssl.create_default_context()
                # Allow self-signed certificates (for development)
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
                ssl_options = pika.SSLOptions(ssl_context, self.host)

            parameters = pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                virtual_host=self.virtual_host,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300,
                connection_attempts=3,
                retry_delay=2,
                socket_timeout=self.connection_timeout,
                ssl_options=ssl_options,
            )

            logger.info(
                f"Attempting to connect to RabbitMQ at {self.host}:{self.port} "
                f"(SSL: {self.use_ssl}, timeout: {self.connection_timeout}s)"
            )

            self.connection = pika.BlockingConnection(parameters)
            self.channel = self.connection.channel()
            logger.info(
                f"✓ Connected to RabbitMQ at {self.host}:{self.port}/{self.virtual_host}"
            )
            return True
        except AMQPConnectionError as e:
            logger.error(
                f"✗ Failed to connect to RabbitMQ at {self.host}:{self.port}: {e}\n"
                f"  Check: host, port, SSL settings, firewall, and credentials"
            )
            return False
        except Exception as e:
            logger.error(f"✗ Unexpected error connecting to RabbitMQ: {e}")
            return False

    def ensure_connection(self):
        """Ensure connection is established, reconnect if needed"""
        if self.connection is None or self.connection.is_closed:
            if not self.connect():
                raise ConnectionError("Failed to establish RabbitMQ connection")

    def publish(
        self,
        queue_name: str,
        message: Dict[str, Any],
        exchange: str = "",
        durable: bool = True,
    ) -> bool:
        """
        Publish a message to a queue

        Args:
            queue_name: Name of the queue
            message: Message dictionary to publish
            exchange: Exchange name (empty string for default exchange)
            durable: Whether the queue should survive broker restarts

        Returns:
            True if message published successfully, False otherwise
        """
        try:
            self.ensure_connection()

            # Declare queue to ensure it exists
            self.channel.queue_declare(queue=queue_name, durable=durable)

            # Publish message
            self.channel.basic_publish(
                exchange=exchange,
                routing_key=queue_name,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # Make message persistent
                    content_type="application/json",
                ),
            )

            logger.debug(f"Published message to queue '{queue_name}': {message}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish message to queue '{queue_name}': {e}")
            return False

    def consume(
        self,
        queue_name: str,
        callback: Callable[
            [
                Dict[str, Any],
                pika.channel.Channel,
                pika.spec.Basic.Deliver,
                pika.BasicProperties,
            ],
            None,
        ],
        durable: bool = True,
        prefetch_count: int = 1,
    ):
        """
        Start consuming messages from a queue

        Args:
            queue_name: Name of the queue to consume from
            callback: Function to call when a message is received
                     Signature: callback(body_dict, channel, method, properties)
            durable: Whether the queue should survive broker restarts
            prefetch_count: Number of unacknowledged messages per consumer
        """
        try:
            self.ensure_connection()

            # Declare queue
            self.channel.queue_declare(queue=queue_name, durable=durable)

            # Set QoS to limit unacknowledged messages
            self.channel.basic_qos(prefetch_count=prefetch_count)

            # Define message handler wrapper
            def on_message(
                channel: pika.channel.Channel,
                method: pika.spec.Basic.Deliver,
                properties: pika.BasicProperties,
                body: bytes,
            ):
                try:
                    message_dict = json.loads(body.decode("utf-8"))
                    callback(message_dict, channel, method, properties)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode message: {e}")
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

            # Start consuming
            self.channel.basic_consume(
                queue=queue_name, on_message_callback=on_message, auto_ack=False
            )

            logger.info(f"Starting to consume messages from queue '{queue_name}'")
            self.channel.start_consuming()

        except KeyboardInterrupt:
            logger.info("Stopping consumer...")
            self.channel.stop_consuming()
        except Exception as e:
            logger.error(f"Error consuming messages from queue '{queue_name}': {e}")
            raise

    def close(self):
        """Close connection to RabbitMQ"""
        try:
            if self.channel and not self.channel.is_closed:
                self.channel.close()
            if self.connection and not self.connection.is_closed:
                self.connection.close()
            logger.info("RabbitMQ connection closed")
        except Exception as e:
            logger.error(f"Error closing RabbitMQ connection: {e}")


# Singleton instance
_rabbitmq_client: Optional[RabbitMQClient] = None


def get_rabbitmq_client() -> RabbitMQClient:
    """Get or create the RabbitMQ client singleton"""
    global _rabbitmq_client
    if _rabbitmq_client is None:
        _rabbitmq_client = RabbitMQClient()
    return _rabbitmq_client
