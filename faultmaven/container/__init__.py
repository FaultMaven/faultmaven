"""Container package for FaultMaven Dependency Injection.

This package provides modular components for the DI container:
- base: Core BaseDIContainer with registry integration
- registry: Dependency tracking and validation
- errors: Container-specific exceptions
- utils: Common utilities for service management
- providers: Service factory modules (infrastructure, services, tools)

Usage:
    from faultmaven.container import container
    await container.initialize()
    llm = container.get_service("llm_provider", required=True)

IMPORTANT: This package consolidates all DI container exports.
The main DIContainer implementation is in faultmaven._container_impl
and re-exported here for clean imports.
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
from faultmaven.container.providers import (
    register_infrastructure,
    register_services,
    register_tools,
)

# Import the full DIContainer implementation and singleton from the impl module
# This ensures all code importing from faultmaven.container gets the proper DIContainer
from faultmaven._container_impl import DIContainer, GlobalContainer, container

__all__ = [
    # Main container classes and singleton
    "DIContainer",
    "GlobalContainer",
    "container",
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
    # Providers
    "register_infrastructure",
    "register_services",
    "register_tools",
]
