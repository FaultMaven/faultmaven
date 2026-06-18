"""Apply LLM configuration overrides from the database to the settings singleton.

Overrides stored in the ``config_overrides`` table take precedence over
environment variables. This module bridges the DB-stored overrides and
the pydantic-settings singleton.

NOTE: the table/columns were generalized for future non-LLM categories
(``config_overrides`` + ``category``/``source``), but the apply + provenance
logic here is still LLM-scoped (``_ALLOWED_OVERRIDES``). Wiring the broader
runtime-safe settings is the documented Phase-2 breadth follow-on.

Usage:
    from faultmaven.config.llm_config_overrides import apply_overrides_to_settings

    # After get_settings() returns, apply DB overrides
    settings = get_settings()
    await apply_overrides_to_settings(settings)
"""

import asyncio
import logging
from typing import Optional

from pydantic import SecretStr

logger = logging.getLogger(__name__)

# Redis key holding a monotonic counter that is bumped on every config write.
# Each replica records the version it has applied (``_last_applied_version``)
# and reloads when it observes a higher value — this is what makes a UI config
# change propagate to ALL cloud replicas, not just the one that served the
# write. Standalone never writes config (the PUT route 403s), so this is inert
# there. See ``watch_config_version`` and ``save_and_reload``.
_CONFIG_VERSION_KEY = "faultmaven:llm_config:version"

# The config version this process has already applied. Process-local on purpose:
# it is the per-replica high-water mark compared against the shared Redis value.
_last_applied_version: Optional[int] = None

# Maps override keys to (settings attribute path, type converter)
# Only keys in this map are accepted — prevents arbitrary setting injection.
_ALLOWED_OVERRIDES = {
    "primary_provider": "provider",
    "strict_provider_mode": "strict_provider_mode",
    "anthropic_api_key": "anthropic_api_key",
    "openai_api_key": "openai_api_key",
    "fireworks_api_key": "fireworks_api_key",
    "groq_api_key": "groq_api_key",
    "gemini_api_key": "gemini_api_key",
    "huggingface_api_key": "huggingface_api_key",
    "cohere_api_key": "cohere_api_key",
    "openrouter_api_key": "openrouter_api_key",
    # Model overrides per provider
    "anthropic_model": "anthropic_model",
    "openai_model": "openai_model",
    "fireworks_model": "fireworks_model",
    "groq_model": "groq_model",
    "gemini_model": "gemini_model",
    "huggingface_model": "huggingface_model",
    "cohere_model": "cohere_model",
    "openrouter_model": "openrouter_model",
    "local_model": "local_model",
}

_API_KEY_FIELDS = {
    "anthropic_api_key",
    "openai_api_key",
    "fireworks_api_key",
    "groq_api_key",
    "gemini_api_key",
    "huggingface_api_key",
    "cohere_api_key",
    "openrouter_api_key",
}


async def get_config_source_map(is_cloud: bool) -> dict[str, str]:
    """Return provenance per overridable key: 'admin-override' vs 'env-default'.

    A key is 'admin-override' when an explicit value is stored in the DB
    (cloud only); otherwise it's 'env-default' (.env / seed). Standalone has no
    DB overrides, so every key is 'env-default'. This lives in the config layer
    (not the API route) so the read of the infrastructure repository does not
    cross the API → infrastructure architecture boundary.
    """
    db_overrides: dict[str, str] = {}
    if is_cloud:
        try:
            from faultmaven.infrastructure.persistence.llm_config_repository import (
                get_all_overrides,
            )

            db_overrides = await get_all_overrides()
        except Exception as e:  # DB may be unavailable; treat as no overrides
            logger.debug(f"Could not read config overrides for provenance: {e}")

    return {
        key: ("admin-override" if key in db_overrides else "env-default")
        for key in _ALLOWED_OVERRIDES
    }


async def apply_overrides_to_settings(settings) -> None:
    """Read overrides from DB and apply them to the LLM settings object.

    Uses object.__setattr__ to bypass pydantic frozen model validation.
    Only applies keys listed in _ALLOWED_OVERRIDES.
    """
    try:
        from faultmaven.infrastructure.persistence.llm_config_repository import (
            get_all_overrides,
        )

        overrides = await get_all_overrides()
    except Exception as e:
        # DB may not be ready (first startup, migrations pending)
        logger.debug(f"Could not read LLM config overrides: {e}")
        return

    if not overrides:
        return

    llm = settings.llm
    applied = []

    for key, value in overrides.items():
        if key not in _ALLOWED_OVERRIDES:
            logger.warning(f"Ignoring unknown LLM config override: {key}")
            continue

        attr_name = _ALLOWED_OVERRIDES[key]

        try:
            if key == "primary_provider":
                from faultmaven.config.settings import LLMProvider

                object.__setattr__(llm, attr_name, LLMProvider(value))
            elif key == "strict_provider_mode":
                object.__setattr__(llm, attr_name, value.lower() in ("true", "1"))
            elif key in _API_KEY_FIELDS:
                object.__setattr__(llm, attr_name, SecretStr(value))
            else:
                object.__setattr__(llm, attr_name, value)
            applied.append(key)
        except Exception as e:
            logger.warning(f"Failed to apply LLM config override {key}: {e}")

    if applied:
        logger.info(f"Applied LLM config overrides: {applied}")


async def reload_llm_config() -> None:
    """Hot-reload LLM configuration from DB overrides.

    Call after writing new overrides to the database. This:
    1. Re-reads overrides from DB
    2. Resets the settings singleton (forces env var reload)
    3. Applies overrides on top of fresh env vars
    4. Resets the provider registry (forces re-initialization)

    In-flight LLM requests may fail during the brief reset window.
    The registry will re-initialize lazily on the next request.
    """
    from faultmaven.config.settings import get_settings, reset_settings
    from faultmaven.infrastructure.llm.providers.registry import reset_registry

    # Reset settings to force fresh env var read
    reset_settings()

    # Get fresh settings and apply DB overrides
    settings = get_settings()
    await apply_overrides_to_settings(settings)

    # Reset registry so it picks up the updated settings
    reset_registry()

    logger.info("LLM configuration reloaded from overrides")


async def _get_redis():
    """Return the SHARED async Redis client for config-version propagation.

    Uses the same cloud Redis the rest of the app uses (settings.database.redis_url).
    Lazy import keeps this module free of an infrastructure import at load time.

    Raises if no shared Redis is available. ``get_async_redis_client`` swallows a
    failed connection (and an unset URL) and falls back to a per-process
    ``FakeRedis`` — which CANNOT propagate across replicas. Silently accepting it
    would make ``save_and_reload`` bump a fake counter (no warning, change never
    reaches other pods) and ``watch_config_version`` poll fake state forever. So
    we reject the fallback and let callers' best-effort error handling fire.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.redis_client import get_async_redis_client

    redis_url = getattr(get_settings().database, "redis_url", None)
    client = await get_async_redis_client(redis_url=redis_url)
    if type(client).__module__.startswith("fakeredis"):
        raise RuntimeError(
            "config propagation requires a shared Redis, but the client resolved "
            "to an in-process FakeRedis (Redis unset or unreachable)"
        )
    return client


async def _read_config_version(redis) -> int:
    """Read the shared config version (0 if never written / key absent)."""
    raw = await redis.get(_CONFIG_VERSION_KEY)
    return int(raw) if raw else 0


async def _aclose(redis) -> None:
    """Close a transient Redis client (best-effort; aclose preferred, close fallback)."""
    closer = getattr(redis, "aclose", None) or getattr(redis, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # pragma: no cover - close failures are not actionable
        pass


async def save_and_reload(
    overrides: dict[str, str], user_id: str | None = None
) -> None:
    """Persist overrides to DB and hot-reload the LLM configuration.

    This is the single entry point for config writes — API routes should
    call this instead of importing the repository directly.

    After applying locally, the shared Redis config version is bumped so every
    OTHER cloud replica's watcher (``watch_config_version``) observes the change
    and reloads too. The bump is best-effort: if Redis is unreachable the local
    write still takes effect on this replica; other replicas would then lag until
    their next restart, which is logged as a warning.
    """
    from faultmaven.infrastructure.persistence.llm_config_repository import (
        set_overrides,
    )

    await set_overrides(overrides, user_id=user_id)
    await reload_llm_config()

    # Propagate to other replicas by bumping the shared version. Record the new
    # value locally so THIS replica's watcher does not redundantly reload. The
    # client is transient (config writes are rare) and closed after use so it
    # does not leak a connection pool — unlike the watcher's long-lived client.
    global _last_applied_version
    try:
        redis = await _get_redis()
        try:
            _last_applied_version = int(await redis.incr(_CONFIG_VERSION_KEY))
        finally:
            await _aclose(redis)
    except Exception as e:
        logger.warning(
            "Applied config locally but could not bump the shared config "
            "version; other replicas may serve stale config until restart: %s",
            e,
        )


async def watch_config_version(poll_interval: float = 10.0) -> None:
    """Background task (cloud, per replica): reload when config changes elsewhere.

    A UI config write hot-reloads only the replica that served it. This watcher
    closes that gap: it seeds the per-replica high-water mark from Redis at
    startup, then polls; when the shared version exceeds what this replica has
    applied, it reloads from the DB. Cancelled on shutdown.

    Best-effort: if Redis cannot be reached at startup the watcher exits (logged)
    rather than spinning — this replica keeps its startup-applied config.
    """
    global _last_applied_version
    try:
        redis = await _get_redis()
        _last_applied_version = await _read_config_version(redis)
    except Exception as e:
        logger.warning(
            "LLM config watcher could not reach Redis; runtime config changes "
            "will not propagate to this replica until restart: %s",
            e,
        )
        return

    logger.info(
        "LLM config watcher started (version=%s, every %ss)",
        _last_applied_version,
        poll_interval,
    )
    while True:
        await asyncio.sleep(poll_interval)
        try:
            current = await _read_config_version(redis)
            if _last_applied_version is None or current > _last_applied_version:
                logger.info(
                    "Detected LLM config change (v%s → v%s); reloading",
                    _last_applied_version,
                    current,
                )
                # Advance the high-water mark only AFTER a successful reload: a
                # failed reload (raises) leaves it unchanged so the next poll
                # retries. The tradeoff is a benign, self-correcting race — if a
                # local save_and_reload bumps the mark during the await above, we
                # overwrite it with the older `current`; the next poll simply sees
                # the newer version and reloads once more (reload is idempotent).
                # Not worth a lock on a 10s best-effort path.
                await reload_llm_config()
                _last_applied_version = current
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("LLM config watch poll failed (will retry): %s", e)
