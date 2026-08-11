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
    get_development_protection_settings,
    get_production_protection_settings,
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
) -> Dict[str, Any]:
    """Setup protection middleware (sync).

    Starlette/FastAPI middleware must be added *before* the application starts.
    This function is intentionally synchronous so it can be called at import-time
    (module initialization) before lifespan/startup executes.

    **Only ``development`` is special; everything else gets production.**
    ``Environment`` has a third member, ``staging``, and this used to route it —
    along with every unrecognised string — to a settings-driven loader gated on
    ``basic_protection_enabled``, whose default was ``False``. A deployment with
    ``ENVIRONMENT=staging`` therefore installed no rate limiting and no
    deduplication at all, silently (fm#1023) — and staging is a deployed
    configuration, not a hypothetical one.

    The routing is now fail-safe in both directions: the default argument is
    ``Environment.PRODUCTION``, and an environment name nobody anticipated lands
    on the strict preset rather than on the permissive one. Loosening protection
    has to be *asked for* by naming ``development`` exactly, which is the only
    value for which the looser numbers and the bypass headers are appropriate.

    The discriminator is the ``Environment`` member rather than a bare literal:
    ``main.py`` passes ``settings.server.environment``, which is an
    ``Environment``, and a rename of the member's value would otherwise leave a
    stale string here that silently stops matching — sending development to the
    production preset. ``Environment`` subclasses ``str``, so plain strings from
    other callers still compare equal.

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
    setup_info: Dict[str, Any] = {
        "protection_enabled": False,
        "middleware_added": [],
        "settings_source": "none",
        "validation": None,
    }

    try:
        # Load settings if not provided
        if settings is None:
            if environment == Environment.DEVELOPMENT:
                settings = get_development_protection_settings()
                setup_info["settings_source"] = "development_defaults"
            else:
                settings = get_production_protection_settings()
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
                    settings.redis_key_prefix = "faultmaven_staging"
        else:
            setup_info["settings_source"] = "provided"

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
