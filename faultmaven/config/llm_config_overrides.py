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

    # After get_settings() returns, apply DB overrides. A failed read raises:
    # it is never read as "no overrides" (#2987).
    settings = get_settings()
    await apply_overrides_to_settings(settings)
"""

import asyncio
import logging
from typing import Optional

from pydantic import SecretStr

from faultmaven.utils.exception_name import exception_log_name

logger = logging.getLogger(__name__)

# Redis key holding a monotonic counter that is bumped on every config write.
# Each replica records the version it has applied (``_last_applied_version``)
# and reloads when it observes a higher value — this is what makes a UI config
# change propagate to ALL cloud replicas, not just the one that served the
# write. Standalone never writes config (the PUT route 403s), so this is inert
# there. See ``watch_config_version`` and ``save_and_reload``.
CONFIG_VERSION_KEY = "faultmaven:llm_config:version"

# The config version this process has already applied. Process-local on purpose:
# it is the per-replica high-water mark compared against the shared Redis value.
_last_applied_version: Optional[int] = None

# The newest generation a reload has taken, and the newest one installed: a
# reload installs only when it is newer than the installed one (#2320), so a
# reload that fails blocks no other (#2987).
_reload_generation = 0
_installed_generation = 0

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
    "azure_api_key": "azure_api_key",
    # Model overrides per provider
    "anthropic_model": "anthropic_model",
    "openai_model": "openai_model",
    "fireworks_model": "fireworks_model",
    "groq_model": "groq_model",
    "gemini_model": "gemini_model",
    "huggingface_model": "huggingface_model",
    "cohere_model": "cohere_model",
    "openrouter_model": "openrouter_model",
    "azure_model": "azure_model",
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
    "azure_api_key",
}


async def get_config_source_map(is_cloud: bool) -> dict[str, str]:
    """Return provenance per overridable key: 'admin-override' vs 'env-default'.

    A key is 'admin-override' when an explicit value is stored in the DB
    (cloud only); otherwise it's 'env-default' (.env / seed). Standalone has no
    DB overrides, so every key is 'env-default'. This lives in the config layer
    (not the API route) so the read of the infrastructure repository does not
    cross the API → infrastructure architecture boundary. Under cloud a failed
    read raises: an unread store is not one with no overrides (#2987).
    """
    db_overrides: dict[str, str] = {}
    if is_cloud:
        from faultmaven.infrastructure.persistence.llm_config_repository import (
            get_all_overrides,
        )

        db_overrides = await get_all_overrides()

    return {
        key: ("admin-override" if key in db_overrides else "env-default")
        for key in _ALLOWED_OVERRIDES
    }


async def apply_overrides_to_settings(settings) -> None:
    """Read overrides from DB and apply them to the LLM settings object.

    Uses object.__setattr__ to bypass pydantic frozen model validation.
    Only applies keys listed in _ALLOWED_OVERRIDES. A failed read raises, so no
    caller installs env-only settings over the operator's (#2987).
    """
    from faultmaven.infrastructure.persistence.llm_config_repository import (
        get_all_overrides,
    )

    overrides = await get_all_overrides()

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
            logger.warning(
                f"Failed to apply LLM config override {key}: {exception_log_name(e)}"
            )

    if applied:
        logger.info(f"Applied LLM config overrides: {applied}")


async def reload_llm_config() -> None:
    """Hot-reload LLM configuration from DB overrides.

    Call after writing new overrides to the database. This:
    1. Builds fresh settings from the env vars, off the singleton
    2. Re-reads overrides from DB and applies them to those settings
    3. Installs them as the singleton, in one step, unless a reload that
       started later has already installed: an older override read that
       returns last never overwrites a newer one
    4. Resets the provider registry (forces re-initialization)

    Until step 3 every reader, the router's per-call timeout included, sees the
    previous settings, never the env-only ones the override read awaits on; a
    build or an override read that raises leaves them installed (#2320, #2987)
    and blocks no other reload. The check and steps 3 and 4 run with no await
    between them. The registry re-initializes lazily on the next request.
    """
    from faultmaven.config.settings import build_settings, install_settings
    from faultmaven.infrastructure.llm.providers.registry import reset_registry

    global _reload_generation, _installed_generation
    settings = build_settings()
    _reload_generation += 1
    generation = _reload_generation
    await apply_overrides_to_settings(settings)

    if generation <= _installed_generation:
        logger.debug("LLM config reload superseded by a newer reload")
        return
    _installed_generation = generation
    install_settings(settings)
    # Reset registry so it picks up the updated settings
    reset_registry()

    logger.info("LLM configuration reloaded from overrides")


async def _get_redis():
    """Return the SHARED async Redis client for config-version propagation.

    Uses the same cloud Redis the rest of the app uses (settings.database.redis_url).
    Lazy import keeps this module free of an infrastructure import at load time.

    Raises if no shared Redis is available. A per-process ``FakeRedis`` CANNOT
    propagate across replicas: accepting one would make ``save_and_reload`` bump
    a fake counter (no warning, change never reaches other pods) and
    ``watch_config_version`` poll fake state forever. Under cloud
    ``get_async_redis_client`` now raises rather than substitute one, so this
    sniff is defense in depth — it still fires on standalone, and it still
    catches a factory regression that lets the substitution through under cloud.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.redis_client import (
        get_async_redis_client,
        is_fakeredis,
    )

    redis_url = getattr(get_settings().database, "redis_url", None)
    client = await get_async_redis_client(redis_url=redis_url)
    if is_fakeredis(client):
        raise RuntimeError(
            "config propagation requires a shared Redis, but the client resolved "
            "to an in-process FakeRedis (Redis unset or unreachable)"
        )
    return client


async def _read_config_version(redis) -> int:
    """Read the shared config version (0 if never written / key absent)."""
    raw = await redis.get(CONFIG_VERSION_KEY)
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

    Once the write lands, the shared Redis config version is bumped so every
    OTHER cloud replica's watcher (``watch_config_version``) observes the change
    and reloads too, even when the local reload raises: the stored value is the
    configuration, and the reload's error is re-raised after the bump (#2987).
    The bump is best-effort: if Redis is unreachable the other replicas lag
    until their next restart, which is logged as a warning.
    """
    from faultmaven.infrastructure.persistence.llm_config_repository import (
        set_overrides,
    )

    await set_overrides(overrides, user_id=user_id)
    reloaded = False
    try:
        await reload_llm_config()
        reloaded = True
    finally:
        await _bump_config_version(reloaded)


async def _bump_config_version(reloaded: bool) -> None:
    """Bump the shared config version so the other replicas reload.

    Records the new value locally only when this replica's reload succeeded, so
    THIS replica's watcher does not redundantly reload, and retries a failed
    one. The client is transient (config writes are rare) and closed after use
    so it does not leak a connection pool — unlike the watcher's long-lived
    client.
    """
    global _last_applied_version
    try:
        redis = await _get_redis()
        try:
            version = int(await redis.incr(CONFIG_VERSION_KEY))
        finally:
            await _aclose(redis)
        if reloaded:
            _last_applied_version = version
    except Exception as e:
        logger.warning(
            "Saved config but could not bump the shared config version; other "
            "replicas may serve stale config until restart: %s",
            exception_log_name(e),
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
            exception_log_name(e),
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
            logger.warning(
                "LLM config watch poll failed (will retry): %s", exception_log_name(e)
            )
