"""
Redis connection client for SafeRoute services.
Automatically detects environment (local dev, Docker, K8s) and configures connection.
"""

import json
import logging
import os
from typing import Any, Optional

import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisClient:
    """Redis client wrapper with automatic environment detection."""

    _instance: Optional["RedisClient"] = None
    _client: Optional[redis.Redis] = None

    def __init__(self):
        """Initialize Redis connection based on environment."""
        # Detect environment
        IS_LOCAL_DEV = os.getenv("LOCAL_DEV", "false").lower() == "true"
        IS_IN_CONTAINER = (
            os.path.exists("/.dockerenv")
            or os.getenv("KUBERNETES_SERVICE_HOST") is not None
        )

        # If not in container and LOCAL_DEV is not explicitly "false", assume local dev
        if not IS_IN_CONTAINER and os.getenv("LOCAL_DEV", "").lower() != "false":
            IS_LOCAL_DEV = True

        # Get Redis configuration from environment variables
        # In K8s: use service name, in local: use localhost
        if IS_LOCAL_DEV:
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))
        else:
            # K8s environment: use service name
            redis_host = os.getenv("REDIS_HOST", "redis.data.svc.cluster.local")
            redis_port = int(os.getenv("REDIS_PORT", "6379"))

        redis_password = os.getenv("REDIS_PASSWORD", None)
        redis_username = os.getenv("REDIS_USERNAME", None)  # Optional: for Redis ACL
        redis_db = int(os.getenv("REDIS_DB", "0"))

        # Create Redis client
        try:
            # Build connection kwargs
            connection_kwargs = {
                "host": redis_host,
                "port": redis_port,
                "db": redis_db,
                "decode_responses": True,  # Automatically decode responses to strings
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
                "retry_on_timeout": True,
                "health_check_interval": 30,
            }

            # Add authentication (username for ACL, password for both legacy and ACL)
            # Note: If Redis has ACL enabled, username is required (often "default")
            # If Redis doesn't have ACL, only password is needed
            # Note: Password with special characters (like @) should work fine with redis-py
            # when passed as separate kwargs, not in URL format
            if redis_username:
                connection_kwargs["username"] = redis_username
            if redis_password:
                # Ensure password is a string and strip any whitespace
                password = str(redis_password).strip()
                connection_kwargs["password"] = password
                # Log password details for debugging (INFO level to see in logs)
                logger.info(
                    f"Redis password length: {len(password)}, "
                    f"starts with: '{password[:3] if len(password) >= 3 else password}', "
                    f"ends with: '{password[-3:] if len(password) >= 3 else password}', "
                    f"repr: {repr(password)}"
                )

            logger.info(
                f"Redis connection kwargs: host={redis_host}, port={redis_port}, "
                f"username={redis_username}, has_password={bool(redis_password)}"
            )

            self._client = redis.Redis(**connection_kwargs)

            # Test connection
            self._client.ping()
            auth_info = (
                f"username={redis_username}" if redis_username else "password only"
            )
            logger.info(
                f"Redis connected: host={redis_host}, port={redis_port}, db={redis_db}, auth={auth_info}"
            )
        except (RedisConnectionError, RedisError) as e:
            logger.error(f"Redis connection failed: {e}")
            logger.error(
                f"Redis config: host={redis_host}, port={redis_port}, db={redis_db}, "
                f"has_username={bool(redis_username)}, has_password={bool(redis_password)}"
            )
            # Provide helpful error message for common issues
            if "AuthenticationError" in str(type(e).__name__):
                if not redis_username:
                    logger.error(
                        "💡 Hint: Redis might require a username (ACL enabled). "
                        "Try setting REDIS_USERNAME=default"
                    )
                else:
                    logger.error(
                        "💡 Hint: Check if username/password pair is correct. "
                        "Password contains special characters that might need escaping."
                    )
            # In development, allow service to start without Redis
            # In production, this should fail
            if not IS_LOCAL_DEV:
                raise

    @classmethod
    def get_instance(cls) -> "RedisClient":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def client(self) -> Optional[redis.Redis]:
        """Get Redis client."""
        return self._client

    def is_connected(self) -> bool:
        """Check if Redis is connected."""
        if self._client is None:
            return False
        try:
            self._client.ping()
            return True
        except (RedisConnectionError, RedisError):
            return False

    def get(self, key: str) -> Optional[str]:
        """Get value from Redis."""
        if not self.is_connected():
            return None
        try:
            return self._client.get(key)
        except (RedisConnectionError, RedisError) as e:
            logger.error(f"Redis GET error for key {key}: {e}")
            return None

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        """Set value in Redis with optional TTL."""
        if not self.is_connected():
            return False
        try:
            if ttl:
                return self._client.setex(key, ttl, value)
            else:
                return self._client.set(key, value)
        except (RedisConnectionError, RedisError) as e:
            logger.error(f"Redis SET error for key {key}: {e}")
            return False

    def delete(self, key: str) -> bool:
        """Delete key from Redis."""
        if not self.is_connected():
            return False
        try:
            return bool(self._client.delete(key))
        except (RedisConnectionError, RedisError) as e:
            logger.error(f"Redis DELETE error for key {key}: {e}")
            return False

    def exists(self, key: str) -> bool:
        """Check if key exists in Redis."""
        if not self.is_connected():
            return False
        try:
            return bool(self._client.exists(key))
        except (RedisConnectionError, RedisError) as e:
            logger.error(f"Redis EXISTS error for key {key}: {e}")
            return False

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """Set JSON value in Redis."""
        try:
            json_str = json.dumps(value, default=str)
            return self.set(key, json_str, ttl)
        except (TypeError, ValueError) as e:
            logger.error(f"JSON serialization error for key {key}: {e}")
            return False

    def get_json(self, key: str) -> Optional[Any]:
        """Get JSON value from Redis."""
        json_str = self.get(key)
        if json_str is None:
            return None
        try:
            return json.loads(json_str)
        except (TypeError, ValueError) as e:
            logger.error(f"JSON deserialization error for key {key}: {e}")
            return None

    def get_ttl(self, key: str) -> Optional[int]:
        """Get remaining TTL for a key."""
        if not self.is_connected():
            return None
        try:
            ttl = self._client.ttl(key)
            return ttl if ttl >= 0 else None
        except (RedisConnectionError, RedisError) as e:
            logger.error(f"Redis TTL error for key {key}: {e}")
            return None


# Global instance getter
def get_redis_client() -> RedisClient:
    """Get Redis client instance."""
    return RedisClient.get_instance()
