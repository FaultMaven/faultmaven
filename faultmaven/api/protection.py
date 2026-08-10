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

from fastapi import APIRouter, FastAPI

from ..config.protection import (
    get_development_protection_settings,
    get_production_protection_settings,
    load_protection_settings,
    validate_protection_settings,
)
from ..infrastructure.protection import TimeoutHandler
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


def create_timeout_handler(
    settings: Optional[ProtectionSettings] = None,
) -> TimeoutHandler:
    """
    Create a timeout handler instance with the given settings

    Args:
        settings: Protection settings (loads from environment if None)

    Returns:
        TimeoutHandler instance
    """
    if settings is None:
        settings = load_protection_settings()

    return TimeoutHandler(settings.timeouts)


def get_protection_health_endpoints():
    """
    Get FastAPI endpoints for protection system health monitoring

    Returns:
        Dictionary of endpoint functions that can be added to FastAPI routers
    """

    async def protection_health():
        """Get overall protection system health"""
        # Imported here rather than at module scope: this module is imported
        # during app assembly, and the Redis factory pulls in settings.
        from ..infrastructure.redis_client import RedisClientFactory

        try:
            settings = load_protection_settings()
            validation = validate_protection_settings(settings)

            return {
                "protection_enabled": settings.enabled,
                "rate_limiting_enabled": settings.rate_limiting_enabled,
                "deduplication_enabled": settings.deduplication_enabled,
                "timeouts_enabled": settings.timeouts.enabled,
                # A configured URL carries its password inline, so it is masked
                # before it leaves the process. ``None`` is the normal case and
                # means the connection resolves centrally.
                "redis_url": (
                    RedisClientFactory._mask_url(settings.redis_url)
                    if settings.redis_url
                    else None
                ),
                "redis_source": (
                    "explicit-url" if settings.redis_url else "central-factory"
                ),
                "validation": validation,
                "status": "healthy" if validation["valid"] else "unhealthy",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def protection_metrics():
        """Get protection system metrics"""
        # This would be populated by the actual middleware instances
        # For now, return a placeholder structure
        return {
            "rate_limiting": {
                "requests_checked": 0,
                "requests_blocked": 0,
                "errors": 0,
            },
            "deduplication": {
                "requests_checked": 0,
                "duplicates_found": 0,
                "cache_hits": 0,
            },
            "timeouts": {
                "total_operations": 0,
                "timeouts_triggered": 0,
                "active_operations": 0,
            },
        }

    async def protection_config():
        """Get current protection configuration"""
        try:
            settings = load_protection_settings()

            # Return sanitized config (no sensitive data)
            return {
                "general": {
                    "enabled": settings.enabled,
                    "fail_open_on_redis_error": settings.fail_open_on_redis_error,
                    "has_bypass_headers": len(settings.protection_bypass_headers) > 0,
                },
                "rate_limiting": {
                    "enabled": settings.rate_limiting_enabled,
                    "limits": {
                        name: {
                            "requests": config.requests,
                            "window": config.window,
                            "enabled": config.enabled,
                        }
                        for name, config in settings.rate_limits.items()
                    },
                },
                "deduplication": {
                    "enabled": settings.deduplication_enabled,
                    "configs": {
                        name: {"ttl": config.ttl, "enabled": config.enabled}
                        for name, config in settings.deduplication.items()
                    },
                },
                "timeouts": {
                    "enabled": settings.timeouts.enabled,
                    "agent_total": settings.timeouts.agent_total,
                    "agent_phase": settings.timeouts.agent_phase,
                    "llm_call": settings.timeouts.llm_call,
                    "emergency_shutdown": settings.timeouts.emergency_shutdown,
                },
            }
        except Exception as e:
            return {"error": str(e), "status": "configuration_error"}

    return {
        "health": protection_health,
        "metrics": protection_metrics,
        "config": protection_config,
    }


def create_protection_router():
    """
    Create a FastAPI router with protection monitoring endpoints

    Returns:
        APIRouter instance with health and metrics endpoints
    """
    router = APIRouter(prefix="/protection", tags=["protection"])
    endpoints = get_protection_health_endpoints()

    router.add_api_route("/health", endpoints["health"], methods=["GET"])
    router.add_api_route("/metrics", endpoints["metrics"], methods=["GET"])
    router.add_api_route("/config", endpoints["config"], methods=["GET"])

    return router
