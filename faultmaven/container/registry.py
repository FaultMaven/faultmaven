"""Dependency Registry for FaultMaven DI Container.

This module provides dependency tracking and validation,
enabling clear service lifecycle management.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

from faultmaven.container.errors import CircularDependencyError, ServiceUnavailableError


class ServiceStatus(Enum):
    """Status of a registered service."""

    PENDING = "pending"  # Not yet initialized
    INITIALIZING = "initializing"  # Currently being initialized
    READY = "ready"  # Successfully initialized
    FAILED = "failed"  # Initialization failed
    DISABLED = "disabled"  # Explicitly disabled


@dataclass
class ServiceInfo:
    """Information about a registered service."""

    name: str
    status: ServiceStatus = ServiceStatus.PENDING
    initialized_at: datetime | None = None
    error: str | None = None
    dependencies: list[str] = field(default_factory=list)
    instance: Any = None

    def is_available(self) -> bool:
        """Check if the service is available for use."""
        return self.status == ServiceStatus.READY and self.instance is not None


class DependencyError(Exception):
    """Error in dependency resolution."""

    pass


T = TypeVar("T")


class DependencyRegistry:
    """Registry for tracking service dependencies and lifecycle.

    This class provides:
    - Service registration with dependency tracking
    - Circular dependency detection
    - Initialization order resolution
    - Service status tracking
    """

    def __init__(self):
        self._services: dict[str, ServiceInfo] = {}
        self._logger = logging.getLogger(__name__)

    def register(
        self,
        name: str,
        dependencies: list[str] | None = None,
    ) -> ServiceInfo:
        """Register a service with its dependencies.

        Args:
            name: Unique service identifier
            dependencies: List of service names this service depends on

        Returns:
            ServiceInfo for the registered service
        """
        if name in self._services:
            return self._services[name]

        info = ServiceInfo(
            name=name,
            dependencies=dependencies or [],
        )
        self._services[name] = info
        return info

    def set_instance(self, name: str, instance: Any) -> None:
        """Set the instance for a registered service.

        Args:
            name: Service name
            instance: The initialized service instance
        """
        if name not in self._services:
            self.register(name)

        info = self._services[name]
        info.instance = instance
        info.status = ServiceStatus.READY
        info.initialized_at = datetime.now(UTC)

    def set_failed(self, name: str, error: str) -> None:
        """Mark a service as failed.

        Args:
            name: Service name
            error: Error message describing the failure
        """
        if name not in self._services:
            self.register(name)

        info = self._services[name]
        info.status = ServiceStatus.FAILED
        info.error = error

    def set_disabled(self, name: str, reason: str | None = None) -> None:
        """Mark a service as explicitly disabled.

        Args:
            name: Service name
            reason: Optional reason for disabling
        """
        if name not in self._services:
            self.register(name)

        info = self._services[name]
        info.status = ServiceStatus.DISABLED
        info.error = reason

    def get(self, name: str) -> Any | None:
        """Get a service instance by name.

        Args:
            name: Service name

        Returns:
            Service instance or None if not available
        """
        info = self._services.get(name)
        if info and info.is_available():
            return info.instance
        return None

    def get_required(self, name: str) -> Any:
        """Get a required service instance.

        Args:
            name: Service name

        Returns:
            Service instance

        Raises:
            ServiceUnavailableError: If service is not available
        """
        instance = self.get(name)
        if instance is None:
            info = self._services.get(name)
            reason = info.error if info else "Service not registered"
            raise ServiceUnavailableError(name, reason)
        return instance

    def get_info(self, name: str) -> ServiceInfo | None:
        """Get service info by name."""
        return self._services.get(name)

    def get_initialization_order(self) -> list[str]:
        """Get services in dependency-respecting order.

        Returns:
            List of service names in initialization order

        Raises:
            CircularDependencyError: If circular dependency detected
        """
        visited: set[str] = set()
        path: list[str] = []
        result: list[str] = []

        def visit(name: str) -> None:
            if name in path:
                cycle_start = path.index(name)
                raise CircularDependencyError(path[cycle_start:] + [name])

            if name in visited:
                return

            path.append(name)
            info = self._services.get(name)
            if info:
                for dep in info.dependencies:
                    visit(dep)

            path.pop()
            visited.add(name)
            result.append(name)

        for name in self._services:
            visit(name)

        return result

    def validate_dependencies(self) -> list[str]:
        """Validate all registered dependencies exist.

        Returns:
            List of missing dependency names
        """
        missing: list[str] = []
        for info in self._services.values():
            for dep in info.dependencies:
                if dep not in self._services:
                    missing.append(dep)
                    self._logger.warning(
                        f"Service '{info.name}' depends on unregistered service '{dep}'"
                    )
        return missing

    def get_all_services(self) -> dict[str, ServiceInfo]:
        """Get all registered services."""
        return dict(self._services)

    def get_ready_services(self) -> list[str]:
        """Get names of all ready services."""
        return [
            name
            for name, info in self._services.items()
            if info.status == ServiceStatus.READY
        ]

    def get_failed_services(self) -> list[tuple[str, str | None]]:
        """Get names and errors of failed services."""
        return [
            (name, info.error)
            for name, info in self._services.items()
            if info.status == ServiceStatus.FAILED
        ]

    def clear(self) -> None:
        """Clear all registered services."""
        self._services.clear()

    def __len__(self) -> int:
        return len(self._services)

    def __contains__(self, name: str) -> bool:
        return name in self._services
