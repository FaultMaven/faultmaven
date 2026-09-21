"""
Request protection setup for FaultMaven

Installs the protection middleware the application actually runs:
- Rate limiting (sliding window, Redis-backed)
- Request deduplication

Both are added before startup so they sit early in the middleware stack; each
resolves its Redis client lazily from ``app.state`` on the first request.
"""

import logging
from typing import Any, Dict, Optional, Union

from fastapi import FastAPI

from ..config.protection import (
    PROTECTION_PROFILE_ENV_VAR,
    STAGING_REDIS_KEY_PREFIX,
    ProtectionProfile,
    get_development_protection_settings,
    get_production_protection_settings,
    resolve_protection_profile,
    resolve_rate_limit_fail_open,
    validate_protection_settings,
)
from ..config.settings import Environment
from ..models.protection import ProtectionSettings
from .middleware import DeduplicationMiddleware, RateLimitMiddleware

logger = logging.getLogger(__name__)


# Middleware setup
def setup_protection_middleware(
    app: FastAPI,
    settings: Optional[ProtectionSettings] = None,
    environment: Union[str, Environment] = Environment.PRODUCTION,
    is_cloud_deployment: bool = False,
) -> Dict[str, Any]:
    """Setup protection middleware (sync).

    Starlette/FastAPI middleware must be added *before* the application starts.
    This function is intentionally synchronous so it can be called at import-time
    (module initialization) before lifespan/startup executes.

    **The preset AND the degrade policy are chosen by the protection profile,
    not by the environment.** ``resolve_protection_profile`` is the whole of
    that decision, and it defaults to ``hardened``: loosening protection has to
    be *asked for* by setting ``PROTECTION_PROFILE=development``.
    ``ENVIRONMENT`` reaches this function for exactly three other purposes —
    the profile's veto (it can refuse a development profile, never select one),
    staging's Redis namespace below, and the empty-trusted-proxies warning — so
    no value of it can install a bypass header and, since fm#1566, no value of
    it decides whether the limiter fails open.

    fm#1566 is the second half of the conflation item 15 named. The degrade
    policy used to be computed here as
    ``for_deployed_environment=(environment != Environment.DEVELOPMENT)``, so a
    self-hosted operator who set ``ENVIRONMENT=production`` — the natural thing
    to do on a production install of a self-hosted product — was silently moved
    to fail-closed on a **single replica**, which also disables the per-replica
    FakeRedis stand-in rung. ``resolve_rate_limit_fail_open`` now answers it
    from the profile, and ``is_cloud_deployment`` (``settings.is_cloud``,
    ADR-004) is what tells a fleet apart from a self-hosted box.

    Note the asymmetry with the bypass-header disarm below, which is
    deliberate: a caller-supplied ``ProtectionSettings`` is **disarmed** of its
    bypass headers but keeps its own ``fail_open_on_redis_error``. The header
    is an unauthenticated opt-out of the whole limiter and no caller may hold
    one on a deployed profile; the degrade policy is a posture, and a caller
    handing in a settings object has stated it. Unchanged by fm#1566, which
    only moved which axis decides it for the presets.

    That is fm#985 item 15: ``ENVIRONMENT`` used to be the discriminator, and
    it is unset on the standalone quickstart — the path every self-hosted
    operator follows — where it falls to the settings default ``development``
    and armed ``X-Dev-Bypass`` / ``X-Test-Bypass``, whose mere *presence* skips
    all rate limiting. ``development`` describes who is editing the code;
    ``standalone`` describes how the product is deployed, and one value cannot
    answer both.

    Before that, this routing was fail-safe in one direction only: it used to
    send ``staging`` — along with every unrecognised string — to a
    settings-driven loader gated on ``basic_protection_enabled``, whose default
    was ``False``, so ``ENVIRONMENT=staging`` installed no rate limiting and no
    deduplication at all, silently (fm#1023). The sweep in
    ``tests/unit/api/test_protection_environment_routing.py`` still pins that
    every environment installs both middlewares.

    **Bypass headers are stripped here, not merely absent from the preset.**
    Whatever the settings came from — either preset, or a caller's own object —
    they are installed carrying no bypass header unless the profile is
    ``development``. Selection alone would only have moved the default: a
    caller handing in a ``ProtectionSettings`` of its own would still have
    armed the headers, and this is the single place every installation of
    ``RateLimitMiddleware`` in the application passes through
    (``tests/unit/api/test_protection_bypass_is_unreachable.py`` ships the scan
    that says so).

    **Fail-closed here is not fail-closed everywhere.** This function refuses —
    it raises on settings that do not validate, and it re-raises anything the
    degrade policy does not cover. The composition root re-mutes that raise for
    exactly one environment: ``main.setup_middleware`` catches it and, when
    ``settings.is_development()`` (which an unset ``ENVIRONMENT`` also satisfies),
    logs an ungated warning and continues with an unprotected app rather than
    refusing to boot. Staging, production and any unrecognised value propagate.
    Read the guarantee as "every deployed environment refuses", not "nothing ever
    boots unprotected".
    """
    profile = resolve_protection_profile(
        environment, is_cloud_deployment=is_cloud_deployment
    )

    setup_info: Dict[str, Any] = {
        "protection_enabled": False,
        "middleware_added": [],
        "settings_source": "none",
        "protection_profile": profile.value,
        "validation": None,
    }

    try:
        # Load settings if not provided
        if settings is None:
            if profile is ProtectionProfile.DEVELOPMENT:
                settings = get_development_protection_settings()
                setup_info["settings_source"] = "development_defaults"
            else:
                # This preset is what every non-development profile installs —
                # the standalone quickstart, a self-hosted deployment and a
                # cloud fleet alike. Its two audience-dependent behaviours are
                # now decided by two DIFFERENT questions, and are passed in
                # separately so that neither can be re-keyed onto the other's
                # axis by accident:
                #
                #   * the degrade policy, from the PROFILE (fm#1566) — a
                #     self-hosted box fails open and keeps its per-replica
                #     FakeRedis stand-in; only a cloud fleet pins fail-closed.
                #   * the empty-trusted-proxies warning, from the ENVIRONMENT,
                #     because "is something proxying this box" is what it asks.
                #     Unchanged by fm#1566: the same set of boxes warns.
                settings = get_production_protection_settings(
                    for_deployed_environment=(environment != Environment.DEVELOPMENT),
                    fail_open_on_redis_error=resolve_rate_limit_fail_open(profile),
                )
                setup_info["settings_source"] = "production_defaults"
                if environment == Environment.STAGING:
                    # Staging runs production's *semantics* — strict limits, no
                    # bypass headers, fail-closed on Redis — but it must not run
                    # in production's *key namespace*. Pointed at one Redis they
                    # would share every rate-limit counter and every dedup key: a
                    # staging load test would consume production's quota, and an
                    # identical request issued in both would be answered 409 in
                    # the second.
                    #
                    # This orphans nothing. Before fm#1023 staging installed no
                    # protection middleware at all, so it has never written a key
                    # under any prefix; there is no existing namespace to migrate
                    # away from.
                    #
                    # Mutated in place rather than copied because the preset
                    # returns a freshly constructed ProtectionSettings on every
                    # call — no other holder can observe this.
                    #
                    # Read from the constant, not spelled here: this namespace
                    # is invisible to anything that discovers prefixes by
                    # calling the preset constructors, so the deployment wipe
                    # can only know about it via `ALL_REDIS_KEY_PREFIXES`
                    # (fm#1052). A literal here would drift out of that set.
                    settings.redis_key_prefix = STAGING_REDIS_KEY_PREFIX
        else:
            setup_info["settings_source"] = "provided"

        if profile is not ProtectionProfile.DEVELOPMENT and (
            settings.protection_bypass_headers
        ):
            # The choke point. ``_should_bypass`` checks header PRESENCE, so a
            # single armed header name is an unauthenticated opt-out of the
            # whole limiter — which makes "the preset does not set them" too
            # weak a guarantee to rest on. Anything that reaches an install
            # with headers on a non-development profile is disarmed here.
            #
            # Copied rather than mutated: the presets return a fresh object on
            # every call, but caller-supplied settings belong to the caller and
            # a silent in-place edit of them is a side effect nobody asked for.
            # ``model_copy(update=...)`` applies the update (unlike the
            # ``deep=True`` form, which shares the dict it was handed).
            disarmed = list(settings.protection_bypass_headers)
            settings = settings.model_copy(update={"protection_bypass_headers": []})
            setup_info["bypass_headers_disarmed"] = disarmed
            logger.warning(
                "Disarmed protection bypass headers %s: this deployment runs "
                "the '%s' protection profile. Header presence alone skips all "
                "rate limiting, so the headers are honoured only under "
                "%s=development on a development environment.",
                disarmed,
                profile.value,
                PROTECTION_PROFILE_ENV_VAR,
            )

        setup_info["bypass_headers"] = list(settings.protection_bypass_headers)

        validation = validate_protection_settings(settings)
        setup_info["validation"] = validation
        if not validation["valid"]:
            # Symmetric with the two branches below: settings we cannot trust
            # buy no more leniency than a preset that raised. Returning here
            # handed back an app with nothing installed — fm#1023's silent
            # unprotected state through a third door.
            #
            # Unreachable from main.py: both presets are static and both
            # validate. This guards caller-supplied settings only.
            raise ValueError(
                f"Protection settings validation failed: {validation['errors']}"
            )

        if not settings.enabled:
            # "No protection middleware anywhere" must never be a silent state.
            # That is exactly what fm#1023 was — an empty middleware stack whose
            # only trace was one line nobody was looking for — and the loader
            # that used to announce it went with the fix. Neither preset can
            # reach this branch (both pin ``enabled=True``), so getting here
            # means a caller handed in its own disabled settings object; that is
            # a deliberate act, and it still deserves to be legible in the logs
            # of the deployment it disarms.
            logger.warning(
                "Protection is DISABLED (ProtectionSettings.enabled=False): "
                "rate limiting and request deduplication will NOT be installed, "
                "deployment-wide. Every client can issue unlimited requests and "
                "an exact resubmit will be processed again. Neither preset "
                "produces this — it can only come from a caller-supplied "
                "settings object."
            )
            return setup_info

        # Add middleware in reverse order (FastAPI adds them as a stack)
        # Last added = first executed
        if settings.deduplication_enabled:
            # No client injected: middleware is constructed at import time, before
            # startup creates Redis. It resolves the client lazily from app.state
            # (wired by the composition root) on the first request.
            app.add_middleware(
                DeduplicationMiddleware,
                settings=settings,
            )
            setup_info["middleware_added"].append("deduplication")

        if settings.rate_limiting_enabled:
            # No URL threaded through: the limiter adopts the composition root's
            # client from app.state on the first request.
            app.add_middleware(
                RateLimitMiddleware,
                settings=settings,
            )
            setup_info["middleware_added"].append("rate_limiting")

        # Reported only once both installs have actually happened. Set before
        # them, the flag was a statement of intent: ``add_middleware`` can raise
        # (Starlette refuses it once the app has started), and a caller that
        # swallowed the failure — the development carve-out in the composition
        # root does exactly that — read back ``protection_enabled: True`` from an
        # app carrying no protection middleware at all. ``middleware_added`` is
        # the corroborating detail; this is the field callers branch on.
        setup_info["protection_enabled"] = True

    except ValueError as e:
        # Settings that do not validate are a configuration defect, not a Redis
        # outage, so ``fail_open_on_redis_error`` has no say over them — and it
        # would say the wrong thing: it defaults to ``True``, so the handler
        # below would swallow the raise above and hand back the very
        # unprotected app it was added to prevent.
        #
        # Logged before the re-raise for the same reason the generic handler
        # logs: the composition root's development carve-out swallows this, so
        # without a line here a development boot with unusable protection
        # settings would say nothing about *why* it is unprotected. The
        # ``setup_info`` write is moot on a raise — the caller never receives the
        # dict — but it keeps the two handlers' shapes identical.
        logger.error(f"Failed to setup protection middleware: {e}")
        setup_info["error"] = str(e)
        raise
    except Exception as e:
        logger.error(f"Failed to setup protection middleware: {e}")
        setup_info["error"] = str(e)
        # ``settings is None`` means the *preset call itself* raised, so nothing
        # ever declared a degrade policy. Swallowing that booted the app with no
        # rate limiting and no deduplication and one ERROR line to say so — the
        # same silent-unprotected state fm#1023 fixed, reached by a different
        # door. An unknown policy is not permission to fail open.
        if settings is None or not settings.fail_open_on_redis_error:
            raise

    return setup_info
