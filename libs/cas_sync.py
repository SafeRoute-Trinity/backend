"""
CAS Sync — Redis pub/sub subscriber for cross-replica state propagation.

Each pod starts a background listener (via ``start()``) that subscribes to
the ``cas:state_changes`` channel.  Incoming events are dispatched to
registered callbacks so replicas can invalidate caches, update in-memory
state, or trigger downstream effects the moment *any* replica commits a
state transition.

Integration::

    from libs.cas_sync import cas_subscriber

    # Register a handler (e.g. in a startup event)
    cas_subscriber.on_change(my_callback)
    await cas_subscriber.start()

    # Shutdown
    await cas_subscriber.stop()

The subscriber also tracks the last-received event timestamp, which the
``/ready`` K8s readiness probe uses to verify the pod is still connected
to the event bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

import redis.asyncio as aioredis

from libs.cas_enforcer import CAS_CHANNEL

logger = logging.getLogger("cas.sync")

StateChangeCallback = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class CASSyncSubscriber:
    """Listens to CAS state-change events published by any replica."""

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._task: Optional[asyncio.Task] = None
        self._callbacks: List[StateChangeCallback] = []
        self._last_event_at: Optional[datetime] = None
        self._running = False

    async def start(self) -> None:
        """Connect to Redis and begin listening in a background task."""
        if self._running:
            return

        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_password = os.getenv("REDIS_PASSWORD") or None

        try:
            self._redis = aioredis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            await self._redis.ping()
        except Exception as exc:
            logger.warning("CAS sync subscriber cannot connect to Redis: %s", exc)
            self._redis = None
            return

        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(CAS_CHANNEL)
        self._running = True
        self._task = asyncio.create_task(self._listen(), name="cas-sync-listener")
        logger.info("CAS sync subscriber started on channel '%s'", CAS_CHANNEL)

    async def _listen(self) -> None:
        """Event loop that dispatches incoming messages to callbacks."""
        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    event = json.loads(message["data"])
                    self._last_event_at = datetime.now(timezone.utc)
                    for cb in self._callbacks:
                        try:
                            await cb(event)
                        except Exception:
                            logger.exception("CAS sync callback error")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("CAS sync listener error — reconnecting in 2s")
                await asyncio.sleep(2)

    def on_change(self, callback: StateChangeCallback) -> None:
        """Register an async callback invoked on every state-change event."""
        self._callbacks.append(callback)

    @property
    def last_event_at(self) -> Optional[datetime]:
        """Timestamp of the most recent event (``None`` if none received)."""
        return self._last_event_at

    @property
    def is_connected(self) -> bool:
        return self._running and self._redis is not None

    async def stop(self) -> None:
        """Unsubscribe and close the connection."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe(CAS_CHANNEL)
            await self._pubsub.aclose()
            self._pubsub = None
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        logger.info("CAS sync subscriber stopped")


cas_subscriber = CASSyncSubscriber()
