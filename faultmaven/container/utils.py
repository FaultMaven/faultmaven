"""Container utilities for FaultMaven DI.

This module provides common utilities for service management,
including lazy initialization and service factories.
"""

from typing import Any, Callable, TypeVar, Generic
from functools import wraps
import logging

T = TypeVar("T")


class LazyService(Generic[T]):
    """Lazy service wrapper that initializes on first access.

    This enables deferred initialization for services that may not
    be needed immediately or have expensive startup costs.

    Usage:
        service = LazyService(lambda: ExpensiveService())
        # Service not created yet
        instance = service.get()  # Now initialized
    """

    def __init__(self, factory: Callable[[], T], name: str | None = None):
        """Initialize lazy service wrapper.

        Args:
            factory: Callable that creates the service instance
            name: Optional name for logging
        """
        self._factory = factory
        self._instance: T | None = None
        self._initialized = False
        self._name = name or factory.__name__ if hasattr(factory, "__name__") else "unknown"
        self._logger = logging.getLogger(__name__)

    def get(self) -> T:
        """Get or create the service instance.

        Returns:
            The service instance
        """
        if not self._initialized:
            self._logger.debug(f"Lazily initializing service: {self._name}")
            self._instance = self._factory()
            self._initialized = True
        return self._instance  # type: ignore

    def is_initialized(self) -> bool:
        """Check if service has been initialized."""
        return self._initialized

    def reset(self) -> None:
        """Reset the service to uninitialized state."""
        self._instance = None
        self._initialized = False


def service_getter(
    attr_name: str,
    fallback: Callable[[], Any] | None = None,
    required: bool = False,
) -> Callable:
    """Decorator factory for standardized service getter methods.

    This creates consistent getter behavior across the container:
    - Returns the service if available
    - Optionally creates fallback for development
    - Optionally raises error if required but unavailable

    Args:
        attr_name: Name of the instance attribute storing the service
        fallback: Optional factory for creating fallback instance
        required: If True, raise error when service unavailable

    Returns:
        Decorator function

    Usage:
        class Container:
            @service_getter("_llm_provider", required=True)
            def get_llm_provider(self):
                pass
    """
    def decorator(method: Callable) -> Callable:
        @wraps(method)
        def wrapper(self) -> Any:
            instance = getattr(self, attr_name, None)

            if instance is not None:
                return instance

            if fallback is not None:
                logger = logging.getLogger(__name__)
                logger.debug(f"Creating fallback for {attr_name}")
                instance = fallback()
                setattr(self, attr_name, instance)
                return instance

            if required:
                from faultmaven.container.errors import ServiceUnavailableError
                raise ServiceUnavailableError(
                    attr_name.lstrip("_"),
                    reason="Service not initialized and no fallback available"
                )

            return None

        return wrapper
    return decorator


def check_dependencies(*deps: str) -> Callable:
    """Decorator to check service dependencies before initialization.

    This ensures required services are available before attempting
    to create a dependent service.

    Args:
        *deps: Names of required service attributes

    Returns:
        Decorator function

    Usage:
        @check_dependencies("redis_client", "vector_store")
        def _create_vector_store(self):
            ...
    """
    def decorator(method: Callable) -> Callable:
        @wraps(method)
        def wrapper(self, *args, **kwargs) -> Any:
            logger = logging.getLogger(__name__)
            missing = []

            for dep in deps:
                if not getattr(self, dep, None):
                    missing.append(dep)

            if missing:
                logger.debug(
                    f"Skipping {method.__name__}: missing dependencies {missing}"
                )
                return None

            return method(self, *args, **kwargs)

        return wrapper
    return decorator


def log_service_status(
    service_name: str,
    success_msg: str | None = None,
    failure_msg: str | None = None,
) -> Callable:
    """Decorator to log service initialization status.

    Args:
        service_name: Name of the service for logging
        success_msg: Custom success message
        failure_msg: Custom failure message

    Returns:
        Decorator function
    """
    def decorator(method: Callable) -> Callable:
        @wraps(method)
        def wrapper(self, *args, **kwargs) -> Any:
            logger = logging.getLogger(__name__)
            try:
                result = method(self, *args, **kwargs)
                if result is not None:
                    msg = success_msg or f"✅ {service_name} initialized"
                    logger.info(msg)
                return result
            except Exception as e:
                msg = failure_msg or f"❌ {service_name} initialization failed: {e}"
                logger.warning(msg)
                raise

        return wrapper
    return decorator
