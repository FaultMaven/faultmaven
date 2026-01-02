"""Container package for FaultMaven Dependency Injection.

This package provides modular components for the DI container:
- registry: Dependency tracking and validation
- errors: Container-specific exceptions
- utils: Common utilities for service management

The main DIContainer class remains in container.py for backward compatibility,
but uses these modular components internally.

Usage:
    from faultmaven.container import container
    await container.initialize()
    service = container.get_session_service()
"""

from faultmaven.container.registry import (
    DependencyRegistry,
    ServiceInfo,
    ServiceStatus,
    DependencyError,
)
from faultmaven.container.errors import (
    ContainerError,
    ServiceUnavailableError,
    InitializationError,
    CircularDependencyError,
    ConfigurationError,
)
from faultmaven.container.utils import (
    LazyService,
    service_getter,
    check_dependencies,
    log_service_status,
)

__all__ = [
    # Registry
    "DependencyRegistry",
    "ServiceInfo",
    "ServiceStatus",
    "DependencyError",
    # Errors
    "ContainerError",
    "ServiceUnavailableError",
    "InitializationError",
    "CircularDependencyError",
    "ConfigurationError",
    # Utils
    "LazyService",
    "service_getter",
    "check_dependencies",
    "log_service_status",
]
