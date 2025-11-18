"""
RabbitMQ connection and messaging helper.
Supports multiple queues and event types.
"""
import os
import json
import pika
import logging
from typing import Callable, Dict, Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)

# RabbitMQ Configuration
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASSWORD = os.getenv("RABBITMQ_PASSWORD", "guest")


# Queue Definitions
class NotificationQueue(str, Enum):
    """Notification queue names with priorities."""
    SOS_EMERGENCY = "notifications.sos.emergency"  # Critical - highest priority
    SAFETY_ALERTS = "notifications.safety.alerts"  # High priority
    FEEDBACK = "notifications.feedback"  # Medium priority
    ROUTING = "notifications.routing"  # Medium priority
    USER_EVENTS = "notifications.user.events"  # Low priority
    GENERAL = "notifications.general"  # Low priority


class NotificationPriority(int, Enum):
    """Message priorities (RabbitMQ supports 0-9)."""
    CRITICAL = 9
    HIGH = 7
    MEDIUM = 5
    LOW = 3
    MINIMAL = 1


class NotificationType(str, Enum):
    """All notification event types."""
    # SOS Events
    EMERGENCY_CALL = "emergency.call"
    EMERGENCY_SMS = "emergency.sms"
    SOS_STATUS_CHANGE = "sos.status.change"
    
    # Safety Events
    LOW_SAFETY_SCORE = "safety.score.low"
    DANGEROUS_AREA = "safety.area.dangerous"
    SAFETY_SCORE_DROP = "safety.score.drop"
    
    # Feedback Events
    CRITICAL_FEEDBACK = "feedback.critical"
    FEEDBACK_RECEIVED = "feedback.received"
    FEEDBACK_STATUS_UPDATE = "feedback.status.update"
    
    # Routing Events
    ROUTE_RECALCULATED = "route.recalculated"
    NAVIGATION_STARTED = "navigation.started"
    NAVIGATION_COMPLETED = "navigation.completed"
    OFF_ROUTE_ALERT = "route.deviation"
    
    # User Events
    USER_REGISTERED = "user.registered"
    PROFILE_UPDATED = "user.profile.updated"
    EMERGENCY_CONTACTS_UPDATED = "user.emergency_contacts.updated"


def get_rabbitmq_connection():
    """Get RabbitMQ connection with retry logic."""
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    return pika.BlockingConnection(parameters)


def publish_notification_event(
    queue: NotificationQueue,
    event_type: NotificationType,
    data: Dict[str, Any],
    priority: NotificationPriority = NotificationPriority.MEDIUM,
    user_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Publish a notification event to RabbitMQ.
    
    Args:
        queue: Target queue name
        event_type: Type of notification event
        data: Event-specific data
        priority: Message priority (0-9, higher = more important)
        user_id: User ID for user-specific notifications
        metadata: Additional metadata
    
    Returns:
        bool: True if published successfully, False otherwise
    """
    try:
        connection = get_rabbitmq_connection()
        channel = connection.channel()
        
        # Declare queue with priority support
        channel.queue_declare(
            queue=queue.value,
            durable=True,
            arguments={"x-max-priority": 10}
        )
        
        # Build message envelope
        message = {
            "event_type": event_type.value,
            "data": data,
            "user_id": user_id,
            "metadata": metadata or {},
            "timestamp": data.get("timestamp") or None
        }
        
        # Publish with priority
        channel.basic_publish(
            exchange='',
            routing_key=queue.value,
            body=json.dumps(message),
            properties=pika.BasicProperties(
                delivery_mode=2,  # persistent
                content_type='application/json',
                priority=priority.value
            )
        )
        
        logger.info(
            f"Published {event_type.value} to queue {queue.value} "
            f"(priority={priority.value}, user={user_id})"
        )
        connection.close()
        return True
        
    except Exception as e:
        logger.error(f"Failed to publish notification event: {e}")
        return False


def consume_messages(queue: NotificationQueue, callback: Callable):
    """
    Consume messages from a RabbitMQ queue.
    Callback should accept (channel, method, properties, body).
    """
    connection = get_rabbitmq_connection()
    channel = connection.channel()
    
    # Declare queue with priority support
    channel.queue_declare(
        queue=queue.value,
        durable=True,
        arguments={"x-max-priority": 10}
    )
    
    # Set QoS - process one message at a time
    channel.basic_qos(prefetch_count=1)
    
    channel.basic_consume(
        queue=queue.value,
        on_message_callback=callback
    )
    
    logger.info(f"Started consuming messages from queue: {queue.value}")
    channel.start_consuming()


# Convenience functions for each service
def publish_sos_event(event_type: NotificationType, data: Dict[str, Any], user_id: str):
    """Publish SOS emergency event (critical priority)."""
    return publish_notification_event(
        NotificationQueue.SOS_EMERGENCY,
        event_type,
        data,
        NotificationPriority.CRITICAL,
        user_id
    )


def publish_safety_alert(event_type: NotificationType, data: Dict[str, Any], user_id: str):
    """Publish safety alert event (high priority)."""
    return publish_notification_event(
        NotificationQueue.SAFETY_ALERTS,
        event_type,
        data,
        NotificationPriority.HIGH,
        user_id
    )


def publish_feedback_event(event_type: NotificationType, data: Dict[str, Any], user_id: str):
    """Publish feedback event (medium priority)."""
    priority = (
        NotificationPriority.HIGH
        if event_type == NotificationType.CRITICAL_FEEDBACK
        else NotificationPriority.MEDIUM
    )
    return publish_notification_event(
        NotificationQueue.FEEDBACK,
        event_type,
        data,
        priority,
        user_id
    )


def publish_routing_event(event_type: NotificationType, data: Dict[str, Any], user_id: str):
    """Publish routing event (medium priority)."""
    return publish_notification_event(
        NotificationQueue.ROUTING,
        event_type,
        data,
        NotificationPriority.MEDIUM,
        user_id
    )


def publish_user_event(event_type: NotificationType, data: Dict[str, Any], user_id: str):
    """Publish user event (low priority)."""
    return publish_notification_event(
        NotificationQueue.USER_EVENTS,
        event_type,
        data,
        NotificationPriority.LOW,
        user_id
    )