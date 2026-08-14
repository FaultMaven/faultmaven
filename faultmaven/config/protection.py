"""
Protection configuration for FaultMaven

**The rate limits, deduplication TTLs and timeouts are code, not configuration.**
They live in the two presets below — development and production — and
``setup_protection_middleware`` chooses between them by environment name. No
environment variable sets a limit, a TTL or a timeout; the loader that once read
per-field variables was unreachable on every healthy deployment and was removed
rather than left looking configurable (fm#1023).

Exactly two environment keys reach these presets:

* ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` — the Redis degrade policy, read by
  ``_fail_open_default``. Honoured by the development preset; the production
  preset pins fail-*closed* and ignores it.
* ``PROTECTION_TRUSTED_PROXIES`` — which proxies' forwarding headers may be
  believed, read by ``get_trusted_proxies``. Honoured by both presets, empty by
  default.

Changing anything else means changing the preset.
"""

import logging
import os
from typing import Any, Dict

from ..models.protection import (
    DeduplicationConfig,
    ProtectionSettings,
    RateLimitConfig,
    TimeoutConfig,
)

logger = logging.getLogger(__name__)

#: Redis key namespace per preset. Named constants rather than literals inside
#: the preset constructors because they have a second consumer: the deployment
#: wipe (``fm-wipe-deployment``) must know EVERY namespace this deployment could
#: have written, and it cannot obtain them by constructing the presets — those
#: call ``get_trusted_proxies()`` / ``_fail_open_default()``, which read settings
#: and emit production warnings as a side effect.
#:
#: ``ALL_REDIS_KEY_PREFIXES`` is the enumeration the wipe consumes. It is the
#: whole set, not the one the *current* ``ENVIRONMENT`` selects, because keys
#: outlive the environment that wrote them: a preset change, an overlay roll, or
#: an execution context that simply does not carry ``ENVIRONMENT`` (the wipe runs
#: with the API scaled down, so it is not the API pod) leaves the other preset's
#: keys on the server. Matching only the current preset made those keys
#: unclassifiable, and ``--verify`` reports what the scoped wipe leaves — so the
#: two agreed with each other while both missed live rate-limit state (fm#1052).
#:
#: ⚠️ There are THREE namespaces and only TWO presets. Staging runs production's
#: preset but is re-pointed to its own namespace by
#: ``api.protection.setup_protection_middleware``, so it cannot be discovered by
#: enumerating the preset constructors. That is exactly why the value lives here
#: and the middleware reads it from here: a fourth namespace introduced the same
#: way must be added to this tuple, or the wipe will not know it either.
DEVELOPMENT_REDIS_KEY_PREFIX = "faultmaven_dev"
PRODUCTION_REDIS_KEY_PREFIX = "faultmaven_prod"
STAGING_REDIS_KEY_PREFIX = "faultmaven_staging"
ALL_REDIS_KEY_PREFIXES = (
    DEVELOPMENT_REDIS_KEY_PREFIX,
    PRODUCTION_REDIS_KEY_PREFIX,
    STAGING_REDIS_KEY_PREFIX,
)


def _fail_open_default() -> bool:
    """Whether the request-path protections fail open when Redis is unreachable.

    ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` (default ``true``) governs the
    rate-limiting and deduplication degrade policy, and nothing else.

    It is deliberately *not* ``PROTECTION_FAIL_OPEN``: that key binds to
    ``settings.protection.fail_open`` and governs PII-redaction fail-open
    (#654, default ``false``). The two policies are independent and must stay
    that way — an operator hardening redaction to fail closed must not thereby
    turn a Redis blip into a 503 on every request.

    One reader, so no producer of a ``ProtectionSettings`` can disagree with
    another about what the deployment asked for.

    ``get_production_protection_settings`` deliberately does *not* call this: it
    pins fail-closed, for the reason given in its docstring.
    """
    return os.getenv("PROTECTION_RATE_LIMIT_FAIL_OPEN", "true").lower() == "true"


def get_trusted_proxies() -> list:
    """Proxies whose forwarding headers may be believed when keying limits.

    ``PROTECTION_TRUSTED_PROXIES`` is a comma-separated list of addresses or
    CIDRs — for a Kubernetes deployment, the ingress controller's pod range.

    **Empty is the default and it is deliberate.** With no entry, no
    ``X-Forwarded-For`` header influences the rate-limit key and every limit is
    keyed on the socket peer. Before this existed the headers were honoured
    unconditionally, so the ``global`` limit — the only one that applies to
    unauthenticated traffic — could be evaded outright by rotating a header the
    limited party controls.

    The cost of the safe default is real and worth stating: a deployment that
    *is* behind a proxy and does not set this keys every request on the
    proxy's address, so all clients share one bucket. It is not silent, and it
    is reported twice over — ``get_production_protection_settings`` warns at
    startup when production leaves it empty, and
    ``client_ip.resolve_client_ip`` warns at request time (throttled) when
    forwarding headers arrive from an address that is not configured here.

    One reader, for the same reason ``_fail_open_default`` is one reader: no
    consumer may disagree with another about which proxies the deployment
    believes. The consumers, in full:

    1. ``get_development_protection_settings`` — populates
       ``ProtectionSettings.trusted_proxies`` for ``RateLimitMiddleware``.
    2. ``get_production_protection_settings`` — the same, and the one preset
       that warns when this key is left empty.
    3. ``PerformanceTrackingMiddleware`` (``api/middleware/performance.py``) —
       so the address a request is *labelled* with cannot disagree with the one
       it is *limited* by.
    4. The OAuth/SSO limiter (``modules/auth/api/rate_limiting.py``), which
       calls this directly rather than reading a ``ProtectionSettings``.
    5. ``LoggingMiddleware`` (``api/middleware/logging.py``), likewise.

    Production honours this key rather than pinning it — unlike the degrade
    policy, there is no value here that is right for every deployment.
    """
    return [
        entry.strip()
        for entry in os.getenv("PROTECTION_TRUSTED_PROXIES", "").split(",")
        if entry.strip()
    ]


def get_development_protection_settings() -> ProtectionSettings:
    """
    Get protection settings optimized for development

    - More lenient rate limits
    - Shorter timeouts for faster feedback
    - Bypass headers enabled
    - Redis degrade policy from ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` (default
      open). Production pins fail-closed instead — see
      ``get_production_protection_settings``.

    Reached only when ``ENVIRONMENT`` is exactly ``development``: every other
    value, including ``staging`` and anything unrecognised, routes to the
    production preset (fm#1023).
    """
    return ProtectionSettings(
        # General
        enabled=True,
        fail_open_on_redis_error=_fail_open_default(),
        protection_bypass_headers=["X-Dev-Bypass", "X-Test-Bypass"],
        trusted_proxies=get_trusted_proxies(),
        # Redis: resolve centrally via RedisClientFactory.
        redis_url=None,
        redis_key_prefix=DEVELOPMENT_REDIS_KEY_PREFIX,
        # Rate limiting (more lenient for development)
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=5000, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=50, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=500, window=3600
            ),
            "per_session_read": RateLimitConfig(enabled=True, requests=600, window=60),
            "per_session_read_hourly": RateLimitConfig(
                enabled=True, requests=6000, window=3600
            ),
            "title_generation": RateLimitConfig(enabled=True, requests=5, window=300),
        },
        # Deduplication (shorter TTLs for faster iteration)
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=30),
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
    - **Fails closed** on a Redis error, and does not read
      ``PROTECTION_RATE_LIMIT_FAIL_OPEN`` — see below

    **This is the default preset, not just production's.** Only
    ``ENVIRONMENT=development`` selects the other one; ``staging`` and any
    unrecognised value land here, so a deployment nobody classified is protected
    rather than unprotected (fm#1023). Read the numbers below as the floor every
    non-development deployment runs on.

    Production is the one preset that pins the degrade policy rather than
    honouring the key, and it pins it *closed*.

    Defaulting it open rests on the claim that the fail-open rung is nearly
    unreachable, because the ladder is shared Redis → per-replica FakeRedis →
    fail open and the first two rungs enforce limits. Both of the defects that
    once made that claim outright false have since been fixed: the sliding
    window counts requests rather than seconds, and the ``global`` key can no
    longer be rotated by a caller sending its own ``X-Forwarded-For``. The
    argument is no longer refuted, so the pin is now a posture decision rather
    than a precondition that has not been met, and it is recorded as one.

    It stays pinned for two reasons.

    First, rung 2 is *per-replica*. FakeRedis is in-process, so during a shared
    Redis outage a deployment of N replicas enforces N independent copies of a
    limit whose configured value only means anything when it is shared, and no
    replica can see a flood spread across its peers. "Limits still enforced" is
    true of rung 2 and materially weaker than it sounds; it is a floor, not a
    substitute.

    Second, the trade this settles — a Redis blip becoming a 503 on every
    request, against a hole in a control that is both a security boundary and a
    cost boundary — is a deliberate choice about which failure production would
    rather have, not a consequence of a bug. Reversing it should be its own
    change, argued on its own evidence, not a rider on whichever fix happens to
    clear the last stated blocker.

    The known cost of the pin is tracked: mid-flight Redis errors are currently
    served as ``429`` with a ``0/0 requests`` body before the ``503`` rung
    engages, which is a confusing way to say "unavailable". That is a defect in
    how the pinned path reports itself, and an argument for fixing the report —
    not for unpinning.

    The development preset does honour ``PROTECTION_RATE_LIMIT_FAIL_OPEN``;
    production opts out explicitly rather than by omission.
    ``PROTECTION_TRUSTED_PROXIES`` is *not* pinned here — unlike the
    degrade policy, no value for it is right for every deployment, and the
    empty default is already the safe one. It is, however, the one preset that
    warns when it is left empty: see below.
    """
    trusted_proxies = get_trusted_proxies()

    if not trusted_proxies:
        # Production is by definition a deployment behind something. Empty here
        # is safe but coarse: every external client resolves to the proxy's own
        # address and shares a single `global` bucket, so one caller crossing
        # 500/60s refuses everyone. The request-time warning in ``client_ip``
        # only fires once forwarding headers actually arrive and is throttled to
        # one per five minutes, which is too late and too quiet to notice during
        # a rollout — so say it once, plainly, at startup.
        #
        # It warns rather than refuses to boot deliberately: an unset value
        # degrades availability, and refusing to start would convert that into
        # a total outage, which is worse than the thing it guards against.
        logger.warning(
            "PROTECTION_TRUSTED_PROXIES is empty in production. Forwarding "
            "headers will be ignored and every rate limit keyed on the socket "
            "peer — behind an ingress that is a single address, so all clients "
            "share one 'global' bucket and one caller can refuse traffic for "
            "everyone. Set it to the proxy's address range (in Kubernetes, the "
            "pod CIDR). Leave it empty only if nothing proxies this service."
        )

    return ProtectionSettings(
        # General
        enabled=True,
        fail_open_on_redis_error=False,
        protection_bypass_headers=[],  # No bypasses in production
        trusted_proxies=trusted_proxies,
        # Redis: resolve centrally via RedisClientFactory.
        redis_url=None,
        redis_key_prefix=PRODUCTION_REDIS_KEY_PREFIX,
        # Rate limiting (strict for production)
        rate_limiting_enabled=True,
        rate_limits={
            "global": RateLimitConfig(enabled=True, requests=500, window=60),
            "per_session": RateLimitConfig(enabled=True, requests=10, window=60),
            "per_session_hourly": RateLimitConfig(
                enabled=True, requests=50, window=3600
            ),
            "per_session_read": RateLimitConfig(enabled=True, requests=120, window=60),
            "per_session_read_hourly": RateLimitConfig(
                enabled=True, requests=1200, window=3600
            ),
            "title_generation": RateLimitConfig(
                enabled=True, requests=1, window=600
            ),  # Once per 10 minutes
        },
        # Deduplication (longer TTLs for better protection)
        deduplication_enabled=True,
        deduplication={
            "default": DeduplicationConfig(enabled=True, ttl=30),
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
