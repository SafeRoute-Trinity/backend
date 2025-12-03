# RabbitMQ Integration Setup

This document describes how RabbitMQ is integrated into the notification system.

## Overview

The notification service now uses RabbitMQ as a message queue layer. When notifications are created, they are published to a RabbitMQ queue instead of being sent directly. A separate worker process consumes messages from the queue and sends them via Twilio (through the SOS service).

## Architecture

```
Notification Service (API)
    ↓ (publishes)
RabbitMQ Queue
    ↓ (consumes)
Notification Worker
    ↓ (sends via)
SOS Service → Twilio
```

## Benefits

1. **Decoupling**: The API doesn't wait for Twilio responses, improving response times
2. **Reliability**: Messages are persisted in the queue, so they won't be lost if the worker is down
3. **Scalability**: Multiple workers can process messages in parallel
4. **Backward Compatibility**: Falls back to direct SOS service calls if RabbitMQ is unavailable

## Configuration

Add the following environment variables to your `.env` file:

```bash
# RabbitMQ Configuration
RABBITMQ_HOST=saferouterabbitmq.duckdns.org
RABBITMQ_PORT=5672
RABBITMQ_USERNAME=your_username
RABBITMQ_PASSWORD=your_password
RABBITMQ_NOTIFICATION_QUEUE=notifications
```

### Default Values

- `RABBITMQ_HOST`: `localhost` (if not set)
- `RABBITMQ_PORT`: `5672` (if not set)
- `RABBITMQ_USERNAME`: `guest` (if not set)
- `RABBITMQ_PASSWORD`: `guest` (if not set)
- `RABBITMQ_NOTIFICATION_QUEUE`: `notifications` (if not set)

## Running the Worker

The notification worker processes messages from the RabbitMQ queue. Run it separately from the API service:

```bash
# From the project root
python services/notification/worker.py
```

Or using uvicorn (if you want to run it as a service):

```bash
# Note: The worker uses blocking connections, so it's designed to run as a standalone script
python services/notification/worker.py
```

## Queue Behavior

- **Queue Name**: `notifications` (configurable via `RABBITMQ_NOTIFICATION_QUEUE`)
- **Durability**: Messages are persisted (survive broker restarts)
- **Message Persistence**: Messages are marked as persistent
- **Prefetch Count**: Worker processes 1 message at a time
- **Retry Logic**: Failed messages are requeued automatically

## Fallback Behavior

If RabbitMQ is unavailable or connection fails:
- The notification service will automatically fall back to direct SOS service calls
- This ensures backward compatibility and prevents service disruption
- Logs will indicate when fallback is used

## Monitoring

Check RabbitMQ management UI at: https://saferouterabbitmq.duckdns.org/#/

You can monitor:
- Queue depth (number of pending messages)
- Message rates (publish/consume)
- Consumer connections
- Message acknowledgments

## Testing

1. **Test with RabbitMQ**:
   ```bash
   # Start the worker
   python services/notification/worker.py
   
   # In another terminal, send a notification via API
   curl -X POST http://localhost:20001/v1/notifications/sos \
     -H "Content-Type: application/json" \
     -d '{"sos_id": "test-123", ...}'
   ```

2. **Test Fallback** (stop RabbitMQ or use wrong credentials):
   - The API will automatically fall back to direct SOS service calls
   - Check logs for fallback messages

## Troubleshooting

### Worker not processing messages
- Check RabbitMQ connection: Verify credentials and host/port
- Check queue exists: Messages might be going to wrong queue
- Check worker logs: Look for connection errors

### Messages stuck in queue
- Check worker is running
- Check worker logs for processing errors
- Verify SOS service is accessible from worker

### High queue depth
- Add more worker instances (scale horizontally)
- Check if SOS service is slow or failing
- Monitor message processing rate

## Production Deployment

For production, consider:
1. Running multiple worker instances for parallel processing
2. Setting up RabbitMQ clustering for high availability
3. Monitoring queue depth and worker health
4. Setting up alerts for queue depth thresholds
5. Using a process manager (systemd, supervisor, etc.) to keep worker running

