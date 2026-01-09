"""Deduplication Store Interface and Implementations.

This module provides an interface for request deduplication storage,
with implementations for Redis and in-memory fallback.

Principle 1 Compliance: Deployment-agnostic interface for deduplication storage.
Services inject IDeduplicationStore rather than directly importing redis.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable


logger = logging.getLogger(__name__)


@runtime_checkable
class IDeduplicationStore(Protocol):
    """Interface for request deduplication storage.

    Implementations must provide atomic check-and-set operations
    with TTL support for detecting duplicate requests.
    """

    async def check_and_set(
        self,
        key: str,
        ttl: int,
        value: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Atomically check if key exists and set if not.

        Args:
            key: Unique key for the request
            ttl: Time-to-live in seconds
            value: Optional value to store (e.g., timestamp)

        Returns:
            Tuple of (is_duplicate, cached_value)
            - is_duplicate: True if key already existed
            - cached_value: The existing value if duplicate, None otherwise
        """
        ...

    async def set_value(
        self,
        key: str,
        value: str,
        ttl: int,
    ) -> None:
        """Set a value with TTL.

        Args:
            key: Key to set
            value: Value to store
            ttl: Time-to-live in seconds
        """
        ...

    async def get_value(self, key: str) -> Optional[str]:
        """Get a value by key.

        Args:
            key: Key to retrieve

        Returns:
            Value if exists, None otherwise
        """
        ...

    async def health_check(self) -> Dict[str, Any]:
        """Check store health.

        Returns:
            Health status dictionary
        """
        ...


class InMemoryDeduplicationStore:
    """In-memory implementation of IDeduplicationStore.

    Provides fallback deduplication when Redis is unavailable.
    Uses a simple dict with periodic cleanup of expired entries.
    """

    def __init__(self, cleanup_interval: int = 60):
        """Initialize in-memory store.

        Args:
            cleanup_interval: Seconds between cleanup runs
        """
        self._store: Dict[str, Tuple[float, Optional[str]]] = {}
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    async def check_and_set(
        self,
        key: str,
        ttl: int,
        value: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Atomically check if key exists and set if not."""
        current_time = time.time()

        # Periodic cleanup
        if current_time - self._last_cleanup > self._cleanup_interval:
            await self._cleanup_expired(ttl)
            self._last_cleanup = current_time

        # Check for existing entry
        if key in self._store:
            timestamp, cached_value = self._store[key]
            if current_time - timestamp < ttl:
                return True, cached_value
            else:
                # Expired
                del self._store[key]

        # Set new entry
        self._store[key] = (current_time, value)
        return False, None

    async def set_value(
        self,
        key: str,
        value: str,
        ttl: int,
    ) -> None:
        """Set a value with TTL."""
        self._store[key] = (time.time(), value)

    async def get_value(self, key: str) -> Optional[str]:
        """Get a value by key."""
        if key in self._store:
            _, value = self._store[key]
            return value
        return None

    async def health_check(self) -> Dict[str, Any]:
        """Check store health."""
        return {
            "status": "healthy",
            "type": "in_memory",
            "entries": len(self._store),
        }

    async def _cleanup_expired(self, default_ttl: int) -> None:
        """Clean up expired entries."""
        current_time = time.time()
        expired_keys = [
            key for key, (timestamp, _) in self._store.items()
            if current_time - timestamp > default_ttl
        ]
        for key in expired_keys:
            del self._store[key]
        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired dedup entries")


class RedisDeduplicationStore:
    """Redis implementation of IDeduplicationStore.

    Uses Redis for distributed deduplication with Lua scripts
    for atomic operations.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str = "faultmaven:dedup",
    ):
        """Initialize Redis store.

        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all keys
        """
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._redis = None
        self._initialized = False
        self._healthy = True

    async def _ensure_initialized(self) -> None:
        """Ensure Redis connection is initialized."""
        if self._initialized:
            return

        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            await self._redis.ping()
            self._healthy = True
            self._initialized = True
            logger.info("Redis deduplication store initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Redis: {e}")
            self._healthy = False
            self._initialized = True
            raise

    async def check_and_set(
        self,
        key: str,
        ttl: int,
        value: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Atomically check if key exists and set if not."""
        await self._ensure_initialized()

        if not self._redis or not self._healthy:
            raise RuntimeError("Redis not available")

        full_key = f"{self._key_prefix}:{key}"
        store_value = value or str(time.time())

        # Lua script for atomic check-and-set
        lua_script = """
        local key = KEYS[1]
        local ttl = tonumber(ARGV[1])
        local value = ARGV[2]

        local existing = redis.call('GET', key)
        if existing then
            return {1, existing}
        end

        redis.call('SETEX', key, ttl, value)
        return {0, nil}
        """

        try:
            result = await self._redis.eval(
                lua_script,
                1,
                full_key,
                ttl,
                store_value,
            )
            is_duplicate, cached_value = result
            return bool(is_duplicate), cached_value
        except Exception as e:
            logger.error(f"Redis check_and_set failed: {e}")
            self._healthy = False
            raise

    async def set_value(
        self,
        key: str,
        value: str,
        ttl: int,
    ) -> None:
        """Set a value with TTL."""
        await self._ensure_initialized()

        if not self._redis or not self._healthy:
            raise RuntimeError("Redis not available")

        full_key = f"{self._key_prefix}:{key}"
        try:
            await self._redis.setex(full_key, ttl, value)
        except Exception as e:
            logger.error(f"Redis set_value failed: {e}")
            self._healthy = False
            raise

    async def get_value(self, key: str) -> Optional[str]:
        """Get a value by key."""
        await self._ensure_initialized()

        if not self._redis or not self._healthy:
            raise RuntimeError("Redis not available")

        full_key = f"{self._key_prefix}:{key}"
        try:
            return await self._redis.get(full_key)
        except Exception as e:
            logger.error(f"Redis get_value failed: {e}")
            self._healthy = False
            raise

    async def health_check(self) -> Dict[str, Any]:
        """Check store health."""
        try:
            await self._ensure_initialized()
            if self._redis:
                await self._redis.ping()
                return {
                    "status": "healthy",
                    "type": "redis",
                    "url": self._redis_url.split("@")[-1],  # Hide credentials
                }
        except Exception as e:
            return {
                "status": "unhealthy",
                "type": "redis",
                "error": str(e),
            }

        return {
            "status": "unhealthy",
            "type": "redis",
            "error": "Not initialized",
        }


def get_deduplication_store(
    redis_url: Optional[str] = None,
    key_prefix: str = "faultmaven:dedup",
    fallback_to_memory: bool = True,
) -> IDeduplicationStore:
    """Factory function to get appropriate deduplication store.

    Args:
        redis_url: Redis connection URL. If None, uses in-memory.
        key_prefix: Prefix for Redis keys
        fallback_to_memory: If True, return in-memory store when Redis unavailable

    Returns:
        IDeduplicationStore implementation
    """
    if redis_url:
        try:
            return RedisDeduplicationStore(redis_url, key_prefix)
        except Exception as e:
            if fallback_to_memory:
                logger.warning(f"Redis unavailable, using in-memory fallback: {e}")
                return InMemoryDeduplicationStore()
            raise

    return InMemoryDeduplicationStore()
