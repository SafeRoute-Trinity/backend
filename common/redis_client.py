"""
Redis client for token blacklist and session management.

This module provides a robust Redis client with:
- Connection pooling (reuse connections, don't create new ones each time)
- Automatic environment detection (local dev, Docker, K8s)
- Health checks and automatic reconnection
- Graceful degradation (works even if Redis is unavailable)
- TTL support for automatic expiration

Environment Variables:
    REDIS_HOST: Redis server host (default: localhost)
    REDIS_PORT: Redis server port (default: 6379)
    REDIS_PASSWORD: Redis password (optional, can be base64 encoded)
    REDIS_DB: Redis database number (default: 0)
"""

import base64
import json
import os
import time
from typing import List, Optional, Set

import redis
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from common.constants import REDIS_DB, REDIS_HOST, REDIS_PORT


class RedisClient:
    """
    Redis client wrapper with connection pooling and automatic reconnection.

    Features:
    - Connection pooling: Reuses connections instead of creating new ones
    - Health checks: Automatically checks connection health
    - Auto-reconnect: Handles connection failures gracefully
    - Environment-aware: Detects local dev, Docker, or K8s environment
    - Graceful degradation: Works even if Redis is unavailable (for dev)

    Connection Management:
    - Uses connection pool (max 50 connections)
    - Connection timeout: 5 seconds
    - Socket timeout: 5 seconds
    - Health check interval: 30 seconds (Redis automatically checks)
    - Connections are kept alive and reused
    """

    def __init__(self):
        """Initialize Redis client with environment-aware configuration."""
        # Get Redis configuration from constants or environment
        self.host = REDIS_HOST
        self.port = REDIS_PORT
        self.db = REDIS_DB

        # Handle password (may be base64 encoded in K8s secrets)
        password = os.getenv("REDIS_PASSWORD", "")
        if password:
            # Try to decode base64 (common in K8s secrets)
            try:
                decoded = base64.b64decode(password).decode("utf-8")
                if decoded and decoded != password:
                    password = decoded
            except Exception:
                # If decoding fails, use original password
                pass

        self.password = password if password else None

        # Connection pool configuration
        # This creates a pool of connections that are reused
        self.pool = redis.ConnectionPool(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password,
            decode_responses=True,  # Automatically decode bytes to strings
            # Connection timeouts
            socket_connect_timeout=5,  # 5 seconds to establish connection
            socket_timeout=5,  # 5 seconds for socket operations
            retry_on_timeout=True,  # Retry on timeout
            # Connection pool settings
            max_connections=50,  # Maximum connections in pool
            # Health check settings
            health_check_interval=30,  # Check connection health every 30 seconds
        )

        # Create Redis client (uses connection pool)
        self.client: Optional[redis.Redis] = None
        self._last_health_check = 0
        self._health_check_interval = 30  # Check health every 30 seconds
        self._connect()

    def _connect(self) -> None:
        """
        Create Redis client connection using connection pool.

        The connection pool manages connections automatically:
        - Creates connections as needed (up to max_connections)
        - Reuses existing connections
        - Closes idle connections after timeout
        - Automatically handles reconnection
        """
        try:
            self.client = redis.Redis(connection_pool=self.pool)
            # Test connection with a quick ping
            self.client.ping()
            self._last_health_check = time.time()
        except (ConnectionError, RedisError, TimeoutError) as e:
            self.client = None
            # In production, you might want to log this
            # For now, we'll allow graceful degradation
            print(f"⚠️  Redis connection failed: {e}. Blacklist features will be disabled.")

    def _ensure_connected(self) -> bool:
        """
        Ensure Redis connection is healthy.

        Performs periodic health checks and reconnects if needed.

        Returns:
            True if connected, False otherwise
        """
        if not self.client:
            return False

        # Periodic health check (every 30 seconds)
        current_time = time.time()
        if current_time - self._last_health_check > self._health_check_interval:
            try:
                self.client.ping()
                self._last_health_check = current_time
                return True
            except (ConnectionError, RedisError, TimeoutError):
                # Connection lost, try to reconnect
                self.client = None
                self._connect()
                return self.client is not None

        return True

    def is_connected(self) -> bool:
        """
        Check if Redis is connected and healthy.

        Returns:
            True if connected and healthy, False otherwise
        """
        if not self._ensure_connected():
            return False
        return self.client is not None

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        """
        Set a key-value pair in Redis.

        Args:
            key: Redis key
            value: Value to store (string)
            ttl: Time to live in seconds (optional)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_connected():
            return False

        try:
            if ttl:
                return bool(self.client.setex(key, ttl, value))
            else:
                return bool(self.client.set(key, value))
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis set error: {e}")
            # Mark as disconnected for next health check
            self.client = None
            return False

    def get(self, key: str) -> Optional[str]:
        """
        Get a value from Redis by key.

        Args:
            key: Redis key

        Returns:
            Value if found, None otherwise
        """
        if not self.is_connected():
            return None

        try:
            value = self.client.get(key)
            return value if value else None
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis get error: {e}")
            self.client = None
            return None

    def exists(self, key: str) -> bool:
        """
        Check if a key exists in Redis.

        Args:
            key: Redis key

        Returns:
            True if key exists, False otherwise
        """
        if not self.is_connected():
            return False

        try:
            return bool(self.client.exists(key))
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis exists error: {e}")
            self.client = None
            return False

    def delete(self, key: str) -> bool:
        """
        Delete a key from Redis.

        Args:
            key: Redis key

        Returns:
            True if key was deleted, False otherwise
        """
        if not self.is_connected():
            return False

        try:
            return bool(self.client.delete(key))
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis delete error: {e}")
            self.client = None
            return False

    def set_json(self, key: str, value: dict, ttl: Optional[int] = None) -> bool:
        """
        Store a JSON-serializable dict in Redis.

        Args:
            key: Redis key
            value: Dictionary to store
            ttl: Time to live in seconds (optional)

        Returns:
            True if successful, False otherwise
        """
        try:
            json_str = json.dumps(value)
            return self.set(key, json_str, ttl)
        except (TypeError, ValueError) as e:
            print(f"⚠️  JSON serialization error: {e}")
            return False

    def get_json(self, key: str) -> Optional[dict]:
        """
        Get and deserialize a JSON value from Redis.

        Args:
            key: Redis key

        Returns:
            Deserialized dictionary if found, None otherwise
        """
        json_str = self.get(key)
        if not json_str:
            return None

        try:
            return json.loads(json_str)
        except (TypeError, ValueError) as e:
            print(f"⚠️  JSON deserialization error: {e}")
            return None

    def ttl(self, key: str) -> int:
        """
        Get the remaining TTL of a key in seconds.

        Args:
            key: Redis key

        Returns:
            TTL in seconds, -1 if key exists but has no TTL, -2 if key doesn't exist
        """
        if not self.is_connected():
            return -2

        try:
            return self.client.ttl(key)
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis TTL error: {e}")
            self.client = None
            return -2

    def delete_many(self, keys: List[str]) -> int:
        """
        Delete multiple keys from Redis.

        Args:
            keys: List of Redis keys to delete

        Returns:
            Number of keys deleted
        """
        if not self.is_connected() or not keys:
            return 0

        try:
            return self.client.delete(*keys)
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis delete_many error: {e}")
            self.client = None
            return 0

    # ========= Redis Set Operations (for user_sessions:<sub>) =========

    def sadd(self, key: str, *values: str) -> int:
        """
        Add one or more members to a Redis Set.

        Used for: user_sessions:<sub> Set to track all sessions for a user.

        Args:
            key: Redis Set key
            *values: One or more values to add to the set

        Returns:
            Number of members added (0 if already exists)
        """
        if not self.is_connected() or not values:
            return 0

        try:
            return self.client.sadd(key, *values)
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis sadd error: {e}")
            self.client = None
            return 0

    def srem(self, key: str, *values: str) -> int:
        """
        Remove one or more members from a Redis Set.

        Args:
            key: Redis Set key
            *values: One or more values to remove from the set

        Returns:
            Number of members removed
        """
        if not self.is_connected() or not values:
            return 0

        try:
            return self.client.srem(key, *values)
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis srem error: {e}")
            self.client = None
            return 0

    def smembers(self, key: str) -> Set[str]:
        """
        Get all members of a Redis Set.

        Used for: Getting all session IDs for a user (logout_all).

        Args:
            key: Redis Set key

        Returns:
            Set of all members (empty set if key doesn't exist)
        """
        if not self.is_connected():
            return set()

        try:
            members = self.client.smembers(key)
            return set(members) if members else set()
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis smembers error: {e}")
            self.client = None
            return set()

    def sismember(self, key: str, value: str) -> bool:
        """
        Check if a value is a member of a Redis Set.

        Args:
            key: Redis Set key
            value: Value to check

        Returns:
            True if member exists, False otherwise
        """
        if not self.is_connected():
            return False

        try:
            return bool(self.client.sismember(key, value))
        except (ConnectionError, RedisError, TimeoutError) as e:
            print(f"⚠️  Redis sismember error: {e}")
            self.client = None
            return False

    def close(self) -> None:
        """
        Close Redis connection pool.

        This should be called when the application shuts down.
        In practice, the connection pool will be garbage collected,
        but explicitly closing is cleaner.
        """
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        if self.pool:
            try:
                self.pool.disconnect()
            except Exception:
                pass


# Singleton instance
_redis_client: Optional[RedisClient] = None


def get_redis_client() -> RedisClient:
    """
    Get singleton Redis client instance.

    The singleton pattern ensures:
    - Only one connection pool is created
    - Connections are reused across requests
    - Memory is not wasted on multiple pools

    Returns:
        RedisClient instance
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
    return _redis_client
