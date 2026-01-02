"""Container package for FaultMaven Dependency Injection.

This package provides modular components for the DI container:
- base: Core BaseDIContainer with registry integration
- registry: Dependency tracking and validation
- errors: Container-specific exceptions
- utils: Common utilities for service management

Usage:
    from faultmaven.container import container
    await container.initialize()
    llm = container.get_service("llm_provider", required=True)
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
from faultmaven.container.base import BaseDIContainer

__all__ = [
    # Base
    "BaseDIContainer",
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
