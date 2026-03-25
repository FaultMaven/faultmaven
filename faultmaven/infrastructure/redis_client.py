"""
Redis client configuration for FaultMaven.

Provides a single factory function that returns either a real Redis client
(cloud/enterprise) or FakeRedis (local deployment). All subsystems receive
the same async Redis interface — no dual code paths needed.

Configuration is read from the unified settings system (faultmaven.config.settings).
"""

import logging
from typing import Optional
from urllib.parse import urlparse

# Conditional Redis import - only available in enterprise edition
try:
    import redis.asyncio as redis

    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Singleton FakeRedis instance shared across all subsystems
_fakeredis_instance = None


def get_fakeredis_client():
    """Return a singleton FakeRedis async client for local deployment.

    All subsystems share one instance so data (sessions, tokens, rate limits)
    is visible across the application, exactly like a real Redis server.
    """
    import fakeredis.aioredis as fakeredis_aio

    global _fakeredis_instance
    if _fakeredis_instance is None:
        _fakeredis_instance = fakeredis_aio.FakeRedis(decode_responses=True)
        logger.info("✅ Redis client: FakeRedis (in-process, local deployment)")
    return _fakeredis_instance


def reset_fakeredis_client():
    """Reset the FakeRedis singleton. For testing only."""
    global _fakeredis_instance
    _fakeredis_instance = None


class RedisClientFactory:
    """Factory for creating configured Redis clients.

    Returns real Redis for cloud, FakeRedis for local deployment.
    """

    @staticmethod
    def create_client(
        redis_url: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        password: Optional[str] = None,
        **kwargs,
    ):
        """Create a Redis client with proper configuration.

        For cloud deployment (redis package installed + config provided):
            Returns a real redis.asyncio.Redis client.
        For local deployment (no redis package or no config):
            Returns a FakeRedis client (full Redis API, in-process).

        Args:
            redis_url: Complete Redis URL (takes precedence)
            host: Redis host
            port: Redis port
            password: Redis password
            **kwargs: Additional Redis client parameters

        Returns:
            Configured async Redis-compatible client
        """
        if not REDIS_AVAILABLE or redis is None:
            return get_fakeredis_client()

        config = RedisClientFactory._build_config(redis_url, host, port, password)

        # Add connection pool settings for better performance
        pool_kwargs = {
            "max_connections": kwargs.pop("max_connections", 20),
            "socket_connect_timeout": kwargs.pop("socket_connect_timeout", 5),
            "socket_timeout": kwargs.pop("socket_timeout", 10),
        }

        try:
            if config["url"]:
                client = redis.from_url(
                    config["url"], decode_responses=True, **pool_kwargs, **kwargs
                )
                logger.info(
                    f"Redis client created from URL: {RedisClientFactory._mask_url(config['url'])}"
                )
            else:
                client = redis.Redis(
                    host=config["host"],
                    port=config["port"],
                    password=config["password"],
                    decode_responses=True,
                    **pool_kwargs,
                    **kwargs,
                )
                logger.info(
                    f"Redis client created: {config['host']}:{config['port']} "
                    f"(auth: {'yes' if config['password'] else 'no'})"
                )

            return client

        except Exception as e:
            logger.warning(f"Failed to create real Redis client: {e}, using FakeRedis")
            return get_fakeredis_client()

    @staticmethod
    def _build_config(
        redis_url: Optional[str],
        host: Optional[str],
        port: Optional[int],
        password: Optional[str],
    ) -> dict:
        """Build Redis configuration from various sources.

        Configuration priority:
        1. Explicit parameters passed to create_client()
        2. Unified settings system (faultmaven.config.settings)
        """
        if redis_url:
            return {"url": redis_url, "host": None, "port": None, "password": None}

        from faultmaven.config.settings import get_settings

        settings = get_settings()
        db_config = settings.database

        if db_config.redis_url:
            return {
                "url": db_config.redis_url,
                "host": None,
                "port": None,
                "password": None,
            }

        config = {
            "url": None,
            "host": host or db_config.redis_host,
            "port": port or db_config.redis_port,
            "password": password
            or (
                db_config.redis_password.get_secret_value()
                if db_config.redis_password is not None
                else None
            ),
        }
        logger.debug(
            f"Built Redis config from settings: {config['host']}:{config['port']}"
        )

        return config

    @staticmethod
    def _mask_url(url: str) -> str:
        """Mask password in URL for logging."""
        try:
            parsed = urlparse(url)
            if parsed.password:
                masked_netloc = parsed.netloc.replace(parsed.password, "***")
                return url.replace(parsed.netloc, masked_netloc)
            return url
        except Exception:
            return url.replace("://", "://***@") if "://" in url else url

    @staticmethod
    async def test_connection(client) -> bool:
        """Test Redis connection health."""
        try:
            response = await client.ping()
            if response:
                logger.info("✅ Redis connection test successful")
                return True
            else:
                logger.error("❌ Redis ping returned False")
                return False
        except Exception as e:
            logger.error(f"❌ Redis connection test failed: {e}")
            return False


def create_redis_client(**kwargs):
    """Convenience function to create a Redis client.

    Always returns a working async Redis-compatible client.
    Falls back to FakeRedis if real Redis is unavailable.
    """
    return RedisClientFactory.create_client(**kwargs)


async def get_async_redis_client(
    redis_url: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> object:
    """Create and validate an async Redis client.

    Attempts to connect to real Redis. If unavailable, returns FakeRedis.
    This is the primary entry point for the DI container.

    Returns:
        A working async Redis-compatible client (never None).
    """
    if REDIS_AVAILABLE and redis is not None and (redis_url or host):
        try:
            if redis_url:
                client = redis.from_url(redis_url, decode_responses=True)
            else:
                client = redis.Redis(
                    host=host, port=port or 6379, decode_responses=True
                )
            await client.ping()
            logger.info(f"✅ Redis client connected @ {redis_url or host}")
            return client
        except Exception as e:
            logger.warning(f"Real Redis unavailable ({e}), using FakeRedis")

    return get_fakeredis_client()


async def validate_redis_connection(client) -> None:
    """Validate Redis connection and log results.

    Raises:
        ConnectionError: If Redis is not accessible
    """
    is_healthy = await RedisClientFactory.test_connection(client)
    if not is_healthy:
        raise ConnectionError("Redis connection validation failed")


# K8s-specific helper (kept for backward compatibility)
def create_k8s_redis_client():
    """Create Redis client configured for K8s cluster."""
    return create_redis_client()
