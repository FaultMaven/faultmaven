"""
Protection configuration for FaultMaven

Loads rate limiting, deduplication, and timeout settings from environment
variables with sensible defaults.
"""

import logging
import os
from datetime import timedelta
from typing import Any, Dict, Optional

from ..models.protection import (
    DeduplicationConfig,
    LimitType,
    ProtectionSettings,
    RateLimitConfig,
    TimeoutConfig,
)

logger = logging.getLogger(__name__)


def _fail_open_default() -> bool:
    """Whether the request-path protections fail open when Redis is unreachable.

    ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` (default ``true``) governs the
    rate-limiting and deduplication degrade policy, and nothing else.

    It is deliberately *not* ``PROTECTION_FAIL_OPEN``: that key binds to
    ``settings.protection.fail_open`` and governs PII-redaction fail-open
    (#654, default ``false``). The two policies are independent and must stay
    that way — an operator hardening redaction to fail closed must not thereby
    turn a Redis blip into a 503 on every request.

    One reader, used by both load paths, so the settings path and the
    environment path cannot disagree about what the deployment asked for; the
    settings path used to hardcode ``True`` and ignore the operator entirely.
    """
    return os.getenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", "true").lower() == "true"


def load_protection_settings(settings=None) -> ProtectionSettings:
    """
    Load protection settings from unified settings or environment variables (fallback).

    Args:
        settings: FaultMavenSettings instance (if None, attempts to load from get_settings())

    Returns:
        ProtectionSettings instance with loaded configuration

    Design Notes:
        - Prefer the settings path. ``_load_from_settings`` is the
          canonical, deployment-agnostic source.
        - Degrade to ``_load_from_environment`` only when settings
          construction itself raises (very early init, or env-var
          validator rejection). That is the *only* way to reach it, so
          the ``RATE_LIMIT_*`` / ``DEDUP_*`` / ``TIMEOUT_*`` env vars it
          reads are dead config on a healthy deployment — the
          settings-side migration for those keys is incomplete (see TODO
          in ``_load_from_environment``).
    """

    if settings is None:
        try:
            from faultmaven.config.settings import get_settings

            settings = get_settings()
        except Exception:
            settings = None

    if settings is not None:
        return _load_from_settings(settings)
    return _load_from_environment()


def _load_from_settings(settings) -> ProtectionSettings:
    """Load protection settings from unified settings"""
    enabled = settings.protection.basic_protection_enabled

    if not enabled:
        # Loud rather than silent. ``setup_protection_middleware`` returns early
        # when this is False, so the whole deployment runs with no rate
        # limiting, no deduplication and no timeout middleware installed
        # anywhere — a state that otherwise produces not one line of output.
        logger.warning(
            "Protection middleware will NOT be installed: "
            "settings.protection.basic_protection_enabled is False (its "
            "default). Rate limiting, deduplication and request timeouts are "
            "all disabled deployment-wide. Set BASIC_PROTECTION_ENABLED=true "
            "to enable them."
        )

    # Basic protection settings are available in the settings
    return ProtectionSettings(
        # General - use protection and database settings.
        #
        # This used to read ``settings.security.protection_enabled``, a field
        # that exists on no settings section at all, so the call raised
        # AttributeError every time and this "canonical" path was dead.
        #
        # The replacement is ``basic_protection_enabled``, not
        # ``protection_enabled``: the latter is the PII/Presidio gate
        # (``redaction.py`` branches on it, and the admin API reports it as
        # ``pii_redaction_enabled``), whereas ``basic_protection_enabled`` is
        # the field ``ProtectionSystem`` already uses to decide whether to
        # install rate limiting and deduplication. Gating middleware on the
        # redaction toggle would be the same one-key-two-meanings defect this
        # branch removed from the fail-open policy.
        enabled=enabled,
        fail_open_on_redis_error=_fail_open_default(),
        protection_bypass_headers=[],  # No bypasses from settings
        # Redis: ``None`` unless an operator set REDIS_URL explicitly, in which
        # case the complete URL genuinely is the configured source. Everything
        # else resolves centrally through RedisClientFactory.
        redis_url=settings.database.redis_url,
        redis_key_prefix="faultmaven",
        # Rate limiting - use defaults since not in basic settings
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=1000, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=10, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=100, window=3600
            ),
            "title_generation": RateLimitConfig(enabled=True, requests=1, window=300),
        },
        # Deduplication - use defaults
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=300),
            "agent_query": DeduplicationConfig(enabled=True, ttl=60),
        },
        # Timeouts - use defaults
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=300,
            agent_phase=120,
            llm_call=30,
            emergency_shutdown=600,
        ),
    )


def _load_from_environment() -> ProtectionSettings:
    """Load protection settings directly from environment variables.

    **This is the settings-construction-failure path, and nothing else.**
    ``load_protection_settings`` reaches it only when ``get_settings()`` itself
    raises — a broken env-var validator, or very early init before settings can
    be built. Every other call goes to ``_load_from_settings``.

    That has a consequence worth stating plainly: the ``RATE_LIMIT_*``,
    ``DEDUP_*`` and ``TIMEOUT_*`` env vars read below are **not** operator
    knobs. An operator who sets them on a healthy deployment changes nothing,
    because the settings path never reads them and uses hardcoded defaults for
    the same values. They are only honoured in the degraded case this function
    exists for. They are kept, rather than deleted, so that a process which has
    already lost its settings still starts from the deployment's intended
    numbers instead of from constants.

    TODO: Promote ``RATE_LIMIT_*``, ``DEDUP_*``, ``TIMEOUT_*`` into
    ``ProtectionSettings`` so the keys become live on the normal path too.
    """

    # Helper function to parse rate limit string
    def parse_rate_limit(
        value: str, default_requests: int, default_window: int
    ) -> RateLimitConfig:
        if not value:
            return RateLimitConfig(
                enabled=True, requests=default_requests, window=default_window
            )

        try:
            requests_str, window_str = value.split(":")
            return RateLimitConfig(
                enabled=True, requests=int(requests_str), window=int(window_str)
            )
        except (ValueError, IndexError):
            return RateLimitConfig(
                enabled=True, requests=default_requests, window=default_window
            )

    # General settings.
    #
    # ``BASIC_PROTECTION_ENABLED``, not ``PROTECTION_ENABLED``: the latter is
    # the PII/Presidio gate, and using it here would let an operator who
    # disabled PII redaction silently lose rate limiting too. The default stays
    # permissive (``true``) rather than matching the settings field's ``false``
    # — this path only runs when settings construction itself failed, and an
    # already-degraded process should keep its request-path protections rather
    # than shed them.
    protection_enabled = os.getenv("BASIC_PROTECTION_ENABLED", "true").lower() == "true"
    fail_open = _fail_open_default()
    bypass_headers = [
        header.strip()
        for header in os.getenv("PROTECTION_BYPASS_HEADERS", "").split(",")
        if header.strip()
    ]

    # Redis: the complete-URL form only, never hand-assembled from REDIS_HOST /
    # REDIS_PORT — that assembly is what dropped REDIS_PASSWORD on the floor.
    # This is the one place ``REDIS_URL`` is read from the environment rather
    # than from settings, because this path exists precisely for when
    # ``get_settings()`` itself raised. ``or None`` is load-bearing: an empty
    # REDIS_URL must read as "not configured", not as a falsy URL that later
    # code treats as an explicit source. ``None`` means resolve centrally.
    redis_url = os.getenv("REDIS_URL") or None
    redis_key_prefix = os.getenv("REDIS_KEY_PREFIX", "faultmaven")

    # Rate limiting settings
    rate_limiting_enabled = os.getenv("RATE_LIMITING_ENABLED", "true").lower() == "true"

    rate_limits = {
        "global": parse_rate_limit(os.getenv("RATE_LIMIT_GLOBAL", "1000:60"), 1000, 60),
        "per_session": parse_rate_limit(
            os.getenv("RATE_LIMIT_PER_SESSION", "10:60"), 10, 60
        ),
        "per_session_hourly": parse_rate_limit(
            os.getenv("RATE_LIMIT_PER_SESSION_HOURLY", "100:3600"), 100, 3600
        ),
        "title_generation": parse_rate_limit(
            os.getenv("RATE_LIMIT_TITLE_GENERATION", "1:300"), 1, 300
        ),
    }

    # Deduplication settings
    deduplication_enabled = os.getenv("DEDUPLICATION_ENABLED", "true").lower() == "true"

    deduplication = {
        "default": DeduplicationConfig(
            enabled=True, ttl=int(os.getenv("DEDUP_DEFAULT_TTL", "300"))
        ),
        "agent_query": DeduplicationConfig(
            enabled=True, ttl=int(os.getenv("DEDUP_AGENT_QUERY_TTL", "60"))
        ),
    }

    # Timeout settings
    timeouts_enabled = os.getenv("TIMEOUTS_ENABLED", "true").lower() == "true"

    timeouts = TimeoutConfig(
        enabled=timeouts_enabled,
        agent_total=int(os.getenv("TIMEOUT_AGENT_TOTAL", "300")),
        agent_phase=int(os.getenv("TIMEOUT_AGENT_PHASE", "120")),
        llm_call=int(os.getenv("TIMEOUT_LLM_CALL", "30")),
        emergency_shutdown=int(os.getenv("TIMEOUT_EMERGENCY_SHUTDOWN", "600")),
    )

    return ProtectionSettings(
        # General
        enabled=protection_enabled,
        fail_open_on_redis_error=fail_open,
        protection_bypass_headers=bypass_headers,
        # Redis
        redis_url=redis_url,
        redis_key_prefix=redis_key_prefix,
        # Rate limiting
        rate_limiting_enabled=rate_limiting_enabled,
        rate_limits=rate_limits,
        # Deduplication
        deduplication_enabled=deduplication_enabled,
        deduplication=deduplication,
        # Timeouts
        timeouts=timeouts,
    )


def get_development_protection_settings() -> ProtectionSettings:
    """
    Get protection settings optimized for development

    - More lenient rate limits
    - Shorter timeouts for faster feedback
    - Bypass headers enabled
    - Redis degrade policy from ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` (default
      open), like every other loader
    """
    return ProtectionSettings(
        # General
        enabled=True,
        fail_open_on_redis_error=_fail_open_default(),
        protection_bypass_headers=["X-Dev-Bypass", "X-Test-Bypass"],
        # Redis: resolve centrally via RedisClientFactory.
        redis_url=None,
        redis_key_prefix="faultmaven_dev",
        # Rate limiting (more lenient for development)
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=5000, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=50, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=500, window=3600
            ),
            "title_generation": RateLimitConfig(enabled=True, requests=5, window=300),
        },
        # Deduplication (shorter TTLs for faster iteration)
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=60),
            "agent_query": DeduplicationConfig(enabled=True, ttl=30),
        },
        # Timeouts (shorter for faster feedback)
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=120,  # 2 minutes
            agent_phase=60,  # 1 minute
            llm_call=20,  # 20 seconds
            emergency_shutdown=180,  # 3 minutes
        ),
    )


def get_production_protection_settings() -> ProtectionSettings:
    """
    Get protection settings optimized for production

    - Strict rate limits
    - Long timeouts for reliability
    - No bypass headers
    - Redis degrade policy from ``PROTECTION_RATE_LIMIT_FAIL_OPEN``, like every
      other loader

    This used to hardcode ``fail_open_on_redis_error=False``, which made
    ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` a no-op in exactly the environment it
    matters in. It now honours the key, and the key's default (``true``) is a
    deliberate posture: the degrade ladder is shared Redis → per-replica
    FakeRedis (limits still enforced, logged at ERROR) → fail open, so the
    fail-open rung is nearly unreachable. Failing closed instead converts a
    Redis blip into a 503 on every request — a total API outage, strictly worse
    than per-replica limiting. An operator who genuinely wants the outage can
    still set the key to ``false``.
    """
    return ProtectionSettings(
        # General
        enabled=True,
        fail_open_on_redis_error=_fail_open_default(),
        protection_bypass_headers=[],  # No bypasses in production
        # Redis: resolve centrally via RedisClientFactory.
        redis_url=None,
        redis_key_prefix="faultmaven_prod",
        # Rate limiting (strict for production)
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=500, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=5, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=50, window=3600
            ),
            "title_generation": RateLimitConfig(
                enabled=True, requests=1, window=600
            ),  # Once per 10 minutes
        },
        # Deduplication (longer TTLs for better protection)
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=600),  # 10 minutes
            "agent_query": DeduplicationConfig(enabled=True, ttl=180),  # 3 minutes
        },
        # Timeouts (longer for reliability)
        timeouts=TimeoutConfig(
            enabled=True,
            agent_total=600,  # 10 minutes
            agent_phase=300,  # 5 minutes
            llm_call=60,  # 1 minute
            emergency_shutdown=1200,  # 20 minutes
        ),
    )


def validate_protection_settings(settings: ProtectionSettings) -> Dict[str, Any]:
    """
    Validate protection settings and return validation report

    Returns:
        Dictionary with validation status and any issues found
    """
    validation = {"valid": True, "warnings": [], "errors": [], "recommendations": []}

    # No Redis check here. ``redis_url`` is ``None`` in the normal case — the
    # connection is resolved centrally by RedisClientFactory, which owns both
    # the credential lookup and the "nothing to connect to" refusal. Requiring
    # a URL here would fail validation on every ordinary deployment and leave
    # the app with no protection middleware installed at all.

    # Check rate limits
    for limit_name, limit_config in settings.rate_limits.items():
        if limit_config.enabled:
            if limit_config.requests <= 0:
                validation["errors"].append(
                    f"Rate limit {limit_name} must have positive request count"
                )
                validation["valid"] = False

            if limit_config.window <= 0:
                validation["errors"].append(
                    f"Rate limit {limit_name} must have positive window"
                )
                validation["valid"] = False

            # Warn about very permissive limits
            if limit_config.requests > 10000:
                validation["warnings"].append(
                    f"Rate limit {limit_name} is very high: {limit_config.requests}"
                )

            # Warn about very restrictive limits
            if limit_config.requests < 5 and limit_name != "title_generation":
                validation["warnings"].append(
                    f"Rate limit {limit_name} is very restrictive: {limit_config.requests}"
                )

    # Check deduplication settings
    for dedup_name, dedup_config in settings.deduplication.items():
        if dedup_config.enabled:
            if dedup_config.ttl <= 0:
                validation["errors"].append(
                    f"Deduplication {dedup_name} must have positive TTL"
                )
                validation["valid"] = False

            # Warn about very long TTLs
            if dedup_config.ttl > 3600:
                validation["warnings"].append(
                    f"Deduplication {dedup_name} TTL is very long: {dedup_config.ttl}s"
                )

    # Check timeout settings
    if settings.timeouts.enabled:
        if settings.timeouts.agent_total <= 0:
            validation["errors"].append("Agent total timeout must be positive")
            validation["valid"] = False

        if settings.timeouts.agent_phase <= 0:
            validation["errors"].append("Agent phase timeout must be positive")
            validation["valid"] = False

        if settings.timeouts.llm_call <= 0:
            validation["errors"].append("LLM call timeout must be positive")
            validation["valid"] = False

        # Check timeout hierarchy
        if settings.timeouts.agent_phase >= settings.timeouts.agent_total:
            validation["warnings"].append(
                "Agent phase timeout should be less than total timeout"
            )

        if settings.timeouts.llm_call >= settings.timeouts.agent_phase:
            validation["warnings"].append(
                "LLM call timeout should be less than phase timeout"
            )

        # Warn about very short timeouts
        if settings.timeouts.llm_call < 10:
            validation["warnings"].append(
                "LLM call timeout is very short, may cause premature failures"
            )

    # Production recommendations
    if not settings.fail_open_on_redis_error:
        if not settings.protection_bypass_headers:
            validation["recommendations"].append(
                "Consider adding emergency bypass headers for production debugging"
            )

    return validation
