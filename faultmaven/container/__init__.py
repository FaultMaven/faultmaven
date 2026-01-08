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

# Global singleton instance
# Note: Will be replaced with DIContainer instance at runtime
# BaseDIContainer is the base class, DIContainer adds initialize()
container = BaseDIContainer()

# Check if interfaces are available for test compatibility
try:
    from faultmaven.models.interfaces import ILLMProvider
    INTERFACES_AVAILABLE = True
except ImportError:
    INTERFACES_AVAILABLE = False

# Lazy import DIContainer to avoid circular dependency
# DIContainer is defined in ../container.py (sibling file)
def _get_dicontainer():
    """Lazy loader for DIContainer class"""
    # Import from parent module's container.py file (not this package)
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "faultmaven._container_impl",
        os.path.join(os.path.dirname(__file__), "..", "container.py")
    )
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.DIContainer
    return BaseDIContainer

# Export for easy access
try:
    DIContainer = _get_dicontainer()
except Exception:
    # Fallback to BaseDIContainer if loading fails
    DIContainer = BaseDIContainer

__all__ = [
    # Base
    "BaseDIContainer",
    "DIContainer",  # Added
    "container",
    "INTERFACES_AVAILABLE",
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
