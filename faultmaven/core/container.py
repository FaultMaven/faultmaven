"""Dependency Injection Container for FaultMaven Services.

This module provides a lightweight DI container for managing service dependencies
and enabling deployment profile separation (community vs enterprise).

Pattern:
- Service registry with lazy initialization
- Singleton pattern for services
- Factory methods for service creation
- Support for testing via clear() method

Usage:
    # Register service factories
    ServiceContainer.register_factory(EmbeddingService, lambda: EmbeddingService())

    # Get service instance (singleton)
    embedding_service = ServiceContainer.get(EmbeddingService)

    # For testing - clear all instances
    ServiceContainer.clear()

Design Reference: Phase 3, Week 14-15 - DI Container Implementation
"""

import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ServiceContainer:
    """Dependency injection container for FaultMaven services.

    This container manages service instances using the singleton pattern,
    allowing for lazy initialization and testability.

    Attributes:
        _instances: Dictionary of service type -> service instance (singletons)
        _factories: Dictionary of service type -> factory function
    """

    _instances: dict[type, Any] = {}
    _factories: dict[type, Callable] = {}
    _lock: bool = False  # Simple lock for thread safety during initialization

    @classmethod
    def register_factory(cls, service_type: type[T], factory: Callable[[], T]) -> None:
        """Register a factory for creating service instances.

        Args:
            service_type: The service class type (e.g., EmbeddingService)
            factory: A callable that returns a new instance of the service

        Example:
            ServiceContainer.register_factory(
                EmbeddingService,
                lambda: EmbeddingService(api_key="...")
            )
        """
        cls._factories[service_type] = factory
        logger.debug(f"Registered factory for {service_type.__name__}")

    @classmethod
    def register_instance(cls, service_type: type[T], instance: T) -> None:
        """Register an existing service instance (useful for testing).

        Args:
            service_type: The service class type
            instance: Pre-configured service instance

        Example:
            # For testing
            mock_service = Mock(spec=EmbeddingService)
            ServiceContainer.register_instance(EmbeddingService, mock_service)
        """
        cls._instances[service_type] = instance
        logger.debug(f"Registered instance for {service_type.__name__}")

    @classmethod
    def get(cls, service_type: type[T]) -> T:
        """Get or create a service instance (singleton pattern).

        If the service has not been created yet, the factory will be called
        and the instance cached for future calls.

        Args:
            service_type: The service class type to retrieve

        Returns:
            Service instance (singleton)

        Raises:
            ValueError: If no factory registered for service_type

        Example:
            embedding_service = ServiceContainer.get(EmbeddingService)
        """
        # Check if instance already exists
        if service_type in cls._instances:
            return cls._instances[service_type]

        # Check if factory exists
        factory = cls._factories.get(service_type)
        if not factory:
            raise ValueError(
                f"No factory registered for {service_type.__name__}. "
                f"Call ServiceContainer.register_factory({service_type.__name__}, factory) first."
            )

        # Create instance using factory
        logger.debug(f"Creating instance of {service_type.__name__}")
        try:
            instance = factory()
            cls._instances[service_type] = instance
            logger.info(f"Created and cached instance of {service_type.__name__}")
            return instance
        except Exception as e:
            logger.error(f"Failed to create instance of {service_type.__name__}: {e}")
            raise

    @classmethod
    def has_factory(cls, service_type: type) -> bool:
        """Check if a factory is registered for a service type.

        Args:
            service_type: The service class type

        Returns:
            True if factory is registered, False otherwise
        """
        return service_type in cls._factories

    @classmethod
    def has_instance(cls, service_type: type) -> bool:
        """Check if an instance exists for a service type.

        Args:
            service_type: The service class type

        Returns:
            True if instance exists, False otherwise
        """
        return service_type in cls._instances

    @classmethod
    def clear(cls) -> None:
        """Clear all service instances (useful for testing).

        This does NOT clear registered factories, only the cached instances.
        Call this between tests to ensure clean state.

        Example:
            def tearDown(self):
                ServiceContainer.clear()
        """
        cleared_count = len(cls._instances)
        cls._instances.clear()
        logger.debug(f"Cleared {cleared_count} service instances")

    @classmethod
    def clear_all(cls) -> None:
        """Clear all service instances AND factories (complete reset).

        This is a complete reset of the container. Use sparingly, typically
        only in test setup/teardown or when completely reconfiguring the app.

        Example:
            def setUpClass(cls):
                ServiceContainer.clear_all()
                # Re-register factories for testing
        """
        instance_count = len(cls._instances)
        factory_count = len(cls._factories)
        cls._instances.clear()
        cls._factories.clear()
        logger.debug(
            f"Cleared {instance_count} instances and {factory_count} factories"
        )

    @classmethod
    def get_registered_services(cls) -> dict[str, bool]:
        """Get status of all registered services (for debugging/health checks).

        Returns:
            Dictionary mapping service name -> has_instance

        Example:
            >>> ServiceContainer.get_registered_services()
            {'EmbeddingService': True, 'VectorStoreService': False}
        """
        status = {}
        for service_type in cls._factories.keys():
            service_name = service_type.__name__
            status[service_name] = service_type in cls._instances
        return status


class ServiceContainerError(Exception):
    """Exception raised for service container errors."""

    pass


class ServiceNotFoundError(ServiceContainerError):
    """Exception raised when a service is not registered."""

    def __init__(self, service_type: type):
        self.service_type = service_type
        super().__init__(
            f"Service {service_type.__name__} is not registered in the container. "
            f"Register it using ServiceContainer.register_factory() first."
        )


class CircularDependencyError(ServiceContainerError):
    """Exception raised when circular dependencies are detected."""

    def __init__(self, service_chain: list):
        self.service_chain = service_chain
        chain_str = " -> ".join([s.__name__ for s in service_chain])
        super().__init__(f"Circular dependency detected: {chain_str}")
