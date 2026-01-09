"""Base container implementation with DependencyRegistry integration.

This module provides the core DIContainer class that uses the modular
registry and utilities for clean dependency injection.
"""

from typing import Any, Optional, List, Dict
import logging
from datetime import datetime, timezone

from faultmaven.container.registry import DependencyRegistry, ServiceStatus
from faultmaven.container.errors import (
    ContainerError,
    ServiceUnavailableError,
    InitializationError,
)
from faultmaven.container.utils import LazyService


class BaseDIContainer:
    """Base dependency injection container with registry integration.

    This class provides:
    - Singleton pattern with proper initialization tracking
    - DependencyRegistry for service lifecycle management
    - Standardized service access patterns
    - Clear error handling with specific exceptions
    """

    _instance: Optional["BaseDIContainer"] = None

    def __new__(cls) -> "BaseDIContainer":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
            cls._instance._initializing = False
            cls._instance._registry = DependencyRegistry()
            cls._instance._logger = logging.getLogger(__name__)
            cls._instance.settings = None  # Will be set during initialization
        return cls._instance

    @property
    def registry(self) -> DependencyRegistry:
        """Get the dependency registry."""
        return self._registry

    @property
    def is_initialized(self) -> bool:
        """Check if container is initialized."""
        return self._initialized

    def initialize(self) -> None:
        """Synchronous initialization stub for base container.

        For full async initialization, use DIContainer.initialize() instead.
        This method creates minimal mock services for testing.
        """
        if self._initialized:
            self._logger.debug("Container already initialized")
            return

        # Create minimal mock services for testing
        from unittest.mock import MagicMock

        # Infrastructure layer mocks
        llm_provider = MagicMock()
        llm_provider.generate = MagicMock(return_value="Mock LLM response")
        llm_provider.generate_response = MagicMock(return_value="Mock LLM response")
        self._register_service("llm_provider", llm_provider)

        sanitizer = MagicMock()
        sanitizer.sanitize = MagicMock(return_value="sanitized data")
        self._register_service("sanitizer", sanitizer)

        tracer = MagicMock()
        tracer.trace = MagicMock()
        self._register_service("tracer", tracer)

        # Tools layer
        self._register_service("tools", [])

        # Service layer mocks
        agent_service = MagicMock()
        agent_service._llm = llm_provider
        agent_service._tools = []
        agent_service._tracer = tracer
        agent_service._sanitizer = sanitizer
        self._register_service("agent_service", agent_service)

        self._initialized = True
        self._logger.info("Base container initialized with mock services")

    def _register_service(
        self,
        name: str,
        instance: Any,
        dependencies: Optional[List[str]] = None,
    ) -> None:
        """Register and set a service instance.

        Args:
            name: Service name (also used as attribute name)
            instance: The service instance
            dependencies: Optional list of dependency names
        """
        self._registry.register(name, dependencies=dependencies)
        self._registry.set_instance(name, instance)
        setattr(self, name, instance)
        self._logger.debug(f"Registered service: {name}")

    def _register_failed(self, name: str, error: str) -> None:
        """Register a service as failed.

        Args:
            name: Service name
            error: Error message
        """
        self._registry.register(name)
        self._registry.set_failed(name, error)
        setattr(self, name, None)
        self._logger.warning(f"Service {name} failed: {error}")

    def _register_disabled(self, name: str, reason: str) -> None:
        """Register a service as disabled.

        Args:
            name: Service name
            reason: Reason for disabling
        """
        self._registry.register(name)
        self._registry.set_disabled(name, reason)
        setattr(self, name, None)
        self._logger.debug(f"Service {name} disabled: {reason}")

    def get_service(self, name: str, required: bool = False) -> Any:
        """Get a service by name.

        Args:
            name: Service name
            required: If True, raise error when unavailable

        Returns:
            Service instance or None

        Raises:
            ServiceUnavailableError: If required=True and service unavailable
        """
        if required:
            return self._registry.get_required(name)
        return self._registry.get(name)

    def has_service(self, name: str) -> bool:
        """Check if a service is available.

        Args:
            name: Service name

        Returns:
            True if service is ready and has an instance
        """
        info = self._registry.get_info(name)
        return info is not None and info.is_available()

    def get_service_status(self, name: str) -> Optional[ServiceStatus]:
        """Get the status of a service.

        Args:
            name: Service name

        Returns:
            ServiceStatus or None if not registered
        """
        info = self._registry.get_info(name)
        return info.status if info else None

    def get_health(self) -> Dict[str, Any]:
        """Get container health status.

        Returns:
            Dict with status and component details
        """
        ready = self._registry.get_ready_services()
        failed = self._registry.get_failed_services()
        all_services = self._registry.get_all_services()

        # Calculate health
        total = len(all_services)
        ready_count = len(ready)
        failed_count = len(failed)

        if failed_count > 0:
            status = "degraded"
        elif ready_count == total and total > 0:
            status = "healthy"
        elif self._initializing:
            status = "initializing"
        elif not self._initialized:
            status = "not_initialized"
        else:
            status = "healthy"  # Initialized but no services is still healthy

        # Create service info dict
        services_info = {
            "total": total,
            "ready": ready_count,
            "failed": failed_count,
        }

        return {
            "status": status,
            "initialized": self._initialized,
            "services": services_info,
            "components": services_info,  # Backward compatibility alias
            "ready_services": ready,
            "failed_services": [name for name, _ in failed],
        }

    def _ensure_initialized_for_getter(self) -> None:
        """Ensure container is initialized before returning services.

        This method is called by getter methods to trigger initialization
        if it hasn't happened yet. Base implementation just calls initialize().
        Subclasses can override with async-aware logic.
        """
        if not self._initialized and not getattr(self, '_initializing', False):
            self.initialize()

    def get_agent_service(self) -> Any:
        """Get the agent service.

        Returns:
            Agent service instance or None if not available
        """
        # Trigger lazy initialization if needed (for tests)
        self._ensure_initialized_for_getter()
        return self.get_service("agent_service", required=False)

    def get_llm_provider(self) -> Any:
        """Get the LLM provider.

        Returns:
            LLM provider instance or None if not available
        """
        # Trigger lazy initialization if needed (for tests)
        self._ensure_initialized_for_getter()
        return self.get_service("llm_provider", required=False)

    def get_sanitizer(self) -> Any:
        """Get the sanitizer service.

        Returns:
            Sanitizer instance or None if not available
        """
        # Trigger lazy initialization if needed (for tests)
        self._ensure_initialized_for_getter()
        return self.get_service("sanitizer", required=False)

    def get_tracer(self) -> Any:
        """Get the tracer service.

        Returns:
            Tracer instance or None if not available
        """
        # Trigger lazy initialization if needed (for tests)
        self._ensure_initialized_for_getter()
        return self.get_service("tracer", required=False)

    def get_tools(self) -> List[Any]:
        """Get the list of tools.

        Returns:
            List of tool instances
        """
        # Trigger lazy initialization if needed (for tests)
        self._ensure_initialized_for_getter()
        tools = self.get_service("tools", required=False)
        return tools if tools is not None else []

    def health_check(self) -> Dict[str, Any]:
        """Alias for get_health() for backward compatibility.

        Returns:
            Dict with status and component details
        """
        return self.get_health()

    def reset(self) -> None:
        """Reset container state.

        Clears all registered services and resets initialization state.
        Useful for testing.
        """
        self._initialized = False
        self._initializing = False

        # Clear all service attributes
        for name in self._registry.get_all_services():
            if hasattr(self, name):
                delattr(self, name)

        self._registry.clear()
        self._logger.info("Container reset complete")

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance completely.

        This is primarily for testing purposes.
        """
        if cls._instance is not None:
            cls._instance.reset()
            cls._instance = None
