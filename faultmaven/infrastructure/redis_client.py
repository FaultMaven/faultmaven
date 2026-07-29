"""
Redis client configuration for FaultMaven.

Provides a single factory function that returns either a real Redis client
(cloud) or FakeRedis (standalone). All subsystems receive
the same async Redis interface — no dual code paths needed.

Configuration is read from the unified settings system (faultmaven.config.settings).
"""

import logging
from typing import Optional
from urllib.parse import urlparse

from faultmaven.config.deployment_coherence import DeploymentCoherenceError

# Conditional Redis import — the redis package is installed for cloud; standalone uses FakeRedis
try:
    import redis.asyncio as redis

    REDIS_AVAILABLE = True
except ImportError:
    redis = None
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Singleton FakeRedis instance shared across all subsystems
_fakeredis_instance = None


class RedisUnavailableError(DeploymentCoherenceError):
    """Raised when Redis is unusable on a deployment that requires a real one.

    A subclass of :class:`DeploymentCoherenceError` — the same boot-refusal
    signal the deployment coherence gate raises, because this is the same
    statement: the running configuration contradicts ``DEPLOYMENT_MODE``.
    """


def fakeredis_or_fail(reason: str):
    """Return the shared FakeRedis, or refuse to run when cloud requires real Redis.

    FakeRedis lives in ONE process. Under ``DEPLOYMENT_MODE=cloud`` the Redis
    store holds deployment-wide state — sessions, token revocation, rate limits,
    request deduplication and idempotency — so substituting an in-process
    stand-in degrades every one of them to per-replica: a revoked token stays
    valid on the other pods, a rate limit is multiplied by the replica count.
    Nothing surfaces the substitution at request time, so cloud fails the boot
    instead of serving a silently weakened deployment. Standalone is
    single-process, where FakeRedis is the intended backend, so it warns.
    """
    from faultmaven.config.settings import get_settings

    if get_settings().is_cloud:
        raise RedisUnavailableError(
            f"Redis is required under DEPLOYMENT_MODE=cloud but is unusable: {reason}. "
            "Refusing to fall back to in-process FakeRedis — sessions, token "
            "revocation and rate limits would silently degrade to per-replica state. "
            "Check REDIS_URL / REDIS_HOST / REDIS_PORT / REDIS_DB / REDIS_PASSWORD."
        )

    logger.warning("Real Redis unavailable (%s), using FakeRedis", reason)
    return get_fakeredis_client()


def _scrub_redis_secrets(reason: str, config: dict) -> str:
    """Strip Redis credentials out of an exception message.

    A construction or connection failure can echo the whole URL back with the
    password inline, and the reason string is both logged and embedded in the
    boot-refusal message — so it is scrubbed before it leaves this module.
    """
    url = config.get("url")
    url_password = None
    if url:
        reason = reason.replace(url, RedisClientFactory._mask_url(url))
        try:
            url_password = urlparse(url).password
        except Exception:  # pragma: no cover - unparseable URLs carry no password
            url_password = None
    for secret in (config.get("password"), url_password):
        if secret:
            reason = reason.replace(secret, "***")
    return reason


def is_fakeredis(client) -> bool:
    """Whether a client is the in-process FakeRedis stand-in, not a real Redis.

    The one predicate for that question — an inline copy in a caller is a copy
    that can drift from this one.
    """
    return type(client).__module__.startswith("fakeredis")


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
        db: Optional[int] = None,
        **kwargs,
    ):
        """Create a Redis client with proper configuration.

        For cloud deployment (redis package installed + config provided):
            Returns a real redis.asyncio.Redis client.
        For local deployment (no redis package or no config):
            Returns a FakeRedis client (full Redis API, in-process) — unless the
            deployment is cloud, where that substitution is fatal
            (see :func:`fakeredis_or_fail`).

        This is the single construction path: pool sizing, socket timeouts and
        the fallback gate live here only, and :func:`get_async_redis_client`
        delegates to it rather than repeating them.

        Args:
            redis_url: Complete Redis URL (takes precedence)
            host: Redis host
            port: Redis port
            password: Redis password
            db: Redis logical database number
            **kwargs: Additional Redis client parameters

        Returns:
            Configured async Redis-compatible client
        """
        if not REDIS_AVAILABLE or redis is None:
            return fakeredis_or_fail("the redis package is not installed")

        config = RedisClientFactory._build_config(redis_url, host, port, password, db)

        # Nothing to connect to. Checked here, on the shared construction path,
        # so both entry points refuse identically (live for a deployment that
        # sets REDIS_HOST explicitly empty; the settings default is non-empty).
        if not config["url"] and not config["host"]:
            return fakeredis_or_fail("no Redis URL or host is configured")

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
                    db=config["db"],
                    decode_responses=True,
                    **pool_kwargs,
                    **kwargs,
                )
                logger.info(
                    f"Redis client created: {config['host']}:{config['port']}"
                    f"/{config['db']} "
                    f"(auth: {'yes' if config['password'] else 'no'})"
                )

            return client

        except Exception as e:
            return fakeredis_or_fail(
                f"client construction failed: {_scrub_redis_secrets(str(e), config)}"
            )

    @staticmethod
    def _build_config(
        redis_url: Optional[str],
        host: Optional[str],
        port: Optional[int],
        password: Optional[str],
        db: Optional[int] = None,
    ) -> dict:
        """Build Redis configuration from various sources.

        The single source of truth for connection parameters — both the sync
        factory and the async entry point resolve through it, so they cannot
        drift into authenticating differently.

        Resolution order:
        1. The explicit ``redis_url`` argument
        2. ``settings.database.redis_url``
        3. The explicit discrete arguments (host/port/password/db)
        4. The ``settings.database`` discrete fields

        A URL wins wholesale: it already carries host, port, password and
        database, so the discrete fields are left unset — and an explicit
        ``db`` or ``password`` argument is ignored whenever a URL wins,
        including when the URL came from settings and the discrete arguments
        were the explicit ones.
        """
        empty = {"host": None, "port": None, "password": None, "db": None}

        if redis_url:
            return {"url": redis_url, **empty}

        from faultmaven.config.settings import get_settings

        settings = get_settings()
        db_config = settings.database

        if db_config.redis_url:
            return {"url": db_config.redis_url, **empty}

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
            # `db or ...` would swallow an explicit 0 — the default database.
            "db": db_config.redis_db if db is None else db,
        }
        logger.debug(
            f"Built Redis config from settings: {config['host']}:{config['port']}"
            f"/{config['db']} (auth: {'yes' if config['password'] else 'no'})"
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

    Returns a working async Redis-compatible client, falling back to FakeRedis
    if real Redis is unavailable — standalone only; cloud raises
    :class:`RedisUnavailableError` rather than degrade to per-replica state.
    """
    return RedisClientFactory.create_client(**kwargs)


def resolve_redis_client(request, injected=None, redis_url: Optional[str] = None):
    """Resolve a working Redis client for request-path middleware.

    Starlette middleware is constructed at import time, before the lifespan
    startup creates Redis — so the client cannot be captured in ``__init__``.
    This resolves it lazily on the first request, in priority order:

    1. ``injected`` — an explicitly provided client (used by tests).
    2. ``app.state.redis_client`` — the single source of truth wired by the
       lifespan composition root (real Redis in cloud, FakeRedis in standalone).
    3. The central factory as a last resort, which returns a working client
       (FakeRedis fallback on standalone; cloud raises rather than degrade).

    Returns a usable client (never None), so callers do not need to
    re-implement the fallback ladder.
    """
    if injected is not None:
        return injected
    client = getattr(request.app.state, "redis_client", None)
    if client is None:
        client = create_redis_client(redis_url=redis_url)
    return client


async def get_async_redis_client(redis_url: Optional[str] = None) -> object:
    """Create an async Redis client and prove it answers before handing it out.

    This is the primary entry point for the DI container. Construction is
    delegated to :meth:`RedisClientFactory.create_client` — one path resolves
    URL/host/port/password/db, sizes the pool and bounds the socket timeouts,
    so the two entry points cannot drift into connecting differently. What this
    adds is the liveness check: a client that cannot ping is not a working
    client, and its pool is released rather than leaked.

    Standalone returns FakeRedis when real Redis is unusable; cloud raises
    :class:`RedisUnavailableError` (see :func:`fakeredis_or_fail`).
    """
    client = RedisClientFactory.create_client(redis_url=redis_url)
    if is_fakeredis(client):
        # Already through the gate: standalone chose the stand-in, cloud raised.
        return client

    try:
        await client.ping()
    except Exception as e:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - close failures are not actionable
            pass
        config = RedisClientFactory._build_config(redis_url, None, None, None)
        return fakeredis_or_fail(_scrub_redis_secrets(str(e), config))

    logger.info("✅ Redis connection verified (ping OK)")
    return client


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
