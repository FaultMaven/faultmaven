"""Observability shim for distributed tracing.

Provides graceful degradation for Opik tracing:
- If Opik installed + ENABLE_TRACING=true: Use Opik
- Otherwise: No-op decorator (do nothing)

Usage:
    from faultmaven.infrastructure.shims.observability import track

    @track("function_name")
    async def my_function():
        # Function code here
        pass
"""

import asyncio
import importlib.util
import logging
import os
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

# Type variable for preserving function signatures
F = TypeVar("F", bound=Callable[..., Any])

# Feature detection WITHOUT importing opik.
#
# `import opik` costs ~16.5s and ~400MB resident (it eagerly pulls its
# evaluation/llm-judges subpackages). Paying that merely to discover whether
# the package exists put the whole cost on every importer of this module,
# including — via shims.metrics and core.investigation — the case persistence
# layer. `find_spec` answers "is it installed?" without executing it. See #849.
#
# The real import is deferred to `track()`, and only when tracing is actually
# switched on. A package that is installed but fails to import now degrades to
# the no-op decorator at that point (with a warning) instead of silently at
# import time — same graceful degradation, reported closer to the cause.
try:
    OPIK_AVAILABLE = importlib.util.find_spec("opik") is not None
except (ImportError, ValueError):  # pragma: no cover - malformed installation
    OPIK_AVAILABLE = False

if OPIK_AVAILABLE:
    logger.info("Opik tracing library available")
else:
    logger.debug("Opik not installed - tracing disabled")


def _resolve_opik_track() -> Optional[Callable[..., Any]]:
    """Import opik's `track` on first use; None if it cannot be loaded."""
    global OPIK_AVAILABLE
    try:
        from opik import track as opik_track

        return opik_track
    except Exception:  # noqa: BLE001 - any import failure must degrade, not raise
        OPIK_AVAILABLE = False
        logger.warning(
            "Opik is installed but failed to import - tracing disabled",
            exc_info=True,
        )
        return None


def _is_tracing_enabled() -> bool:
    """Check if tracing is enabled via environment variable."""
    return os.getenv("ENABLE_TRACING", "false").lower() == "true"


def track(name: str) -> Callable[[F], F]:
    """
    Decorator for distributed tracing.

    If Opik is available and ENABLE_TRACING=true, uses Opik tracking.
    Otherwise, returns a no-op decorator that does nothing.

    Args:
        name: Name of the operation to track

    Returns:
        Decorator function (Opik or no-op)

    Example:
        @track("case_creation")
        async def create_case(data):
            # Your code here
            pass

        @track("sync_operation")
        def sync_function():
            # Synchronous function also supported
            pass
    """
    tracing_enabled = _is_tracing_enabled()

    opik_track = _resolve_opik_track() if OPIK_AVAILABLE and tracing_enabled else None
    if opik_track is not None:
        # Use real Opik tracking
        logger.debug(f"Tracking enabled for: {name}")
        return opik_track(name=name)
    else:
        # Return no-op decorator
        def no_op_decorator(func: F) -> F:
            """No-op decorator that preserves function behavior."""
            if asyncio.iscoroutinefunction(func):

                @wraps(func)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    return await func(*args, **kwargs)

                return async_wrapper  # type: ignore
            else:

                @wraps(func)
                def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                    return func(*args, **kwargs)

                return sync_wrapper  # type: ignore

        logger.debug(
            f"Tracking disabled for: {name} "
            f"(OPIK_AVAILABLE={OPIK_AVAILABLE}, ENABLE_TRACING={tracing_enabled})"
        )
        return no_op_decorator


def get_tracing_status() -> dict:
    """
    Get current tracing status for diagnostics.

    Returns:
        Dictionary with tracing configuration status:
        - opik_available: Whether the Opik library is installed
        - tracing_enabled: Whether ENABLE_TRACING env var is true
        - active: Whether tracing is actually active (both conditions met)
    """
    tracing_enabled = _is_tracing_enabled()
    return {
        "opik_available": OPIK_AVAILABLE,
        "tracing_enabled": tracing_enabled,
        "active": OPIK_AVAILABLE and tracing_enabled,
    }


def is_tracing_active() -> bool:
    """
    Check if tracing is currently active.

    Returns:
        True if Opik is available AND ENABLE_TRACING=true
    """
    return OPIK_AVAILABLE and _is_tracing_enabled()
