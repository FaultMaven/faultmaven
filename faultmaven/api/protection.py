"""
Request protection setup for FaultMaven

Installs the protection middleware the application actually runs:
- Rate limiting (sliding window, Redis-backed)
- Request deduplication

Both are added before startup so they sit early in the middleware stack; each
resolves its Redis client lazily from ``app.state`` on the first request.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI

from ..config.protection import (
    get_development_protection_settings,
    get_production_protection_settings,
    load_protection_settings,
    validate_protection_settings,
)
from ..models.protection import ProtectionSettings
from .middleware import DeduplicationMiddleware, RateLimitMiddleware

logger = logging.getLogger(__name__)


# Middleware setup
def setup_protection_middleware(
    app: FastAPI,
    settings: Optional[ProtectionSettings] = None,
    environment: str = "development",
) -> Dict[str, Any]:
    """Setup protection middleware (sync).

    Starlette/FastAPI middleware must be added *before* the application starts.
    This function is intentionally synchronous so it can be called at import-time
    (module initialization) before lifespan/startup executes.
    """
    setup_info: Dict[str, Any] = {
        "protection_enabled": False,
        "middleware_added": [],
        "settings_source": "none",
        "validation": None,
        "warnings": [],
    }

    try:
        # Load settings if not provided
        if settings is None:
            if environment == "production":
                settings = get_production_protection_settings()
                setup_info["settings_source"] = "production_defaults"
            elif environment == "development":
                settings = get_development_protection_settings()
                setup_info["settings_source"] = "development_defaults"
            else:
                settings = load_protection_settings()
                setup_info["settings_source"] = "environment"
        else:
            setup_info["settings_source"] = "provided"

        validation = validate_protection_settings(settings)
        setup_info["validation"] = validation
        if not validation["valid"]:
            logger.error(
                f"Protection settings validation failed: {validation['errors']}"
            )
            return setup_info

        if not settings.enabled:
            return setup_info

        setup_info["protection_enabled"] = True

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

    except Exception as e:
        logger.error(f"Failed to setup protection middleware: {e}")
        setup_info["error"] = str(e)
        if settings and not settings.fail_open_on_redis_error:
            raise

    return setup_info
