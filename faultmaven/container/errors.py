"""Container-specific exceptions for FaultMaven DI.

This module provides structured exceptions for container operations,
enabling clear error handling and debugging.
"""


class ContainerError(Exception):
    """Base exception for all container-related errors."""

    def __init__(self, message: str, service_name: str | None = None):
        self.service_name = service_name
        super().__init__(message)


class ServiceUnavailableError(ContainerError):
    """Raised when a requested service is not available.

    This can occur when:
    - The service failed to initialize
    - Required dependencies are missing
    - The service was explicitly disabled via configuration
    """

    def __init__(self, service_name: str, reason: str | None = None):
        message = f"Service '{service_name}' is not available"
        if reason:
            message += f": {reason}"
        super().__init__(message, service_name=service_name)
        self.reason = reason


class InitializationError(ContainerError):
    """Raised when container or service initialization fails.

    This indicates a critical failure during startup that
    prevents the application from functioning correctly.
    """

    def __init__(
        self,
        message: str,
        service_name: str | None = None,
        cause: Exception | None = None,
    ):
        super().__init__(message, service_name=service_name)
        self.cause = cause
        self.__cause__ = cause


class CircularDependencyError(ContainerError):
    """Raised when a circular dependency is detected.

    This indicates a configuration error where services
    depend on each other in a way that cannot be resolved.
    """

    def __init__(self, dependency_chain: list[str]):
        self.dependency_chain = dependency_chain
        chain_str = " -> ".join(dependency_chain)
        message = f"Circular dependency detected: {chain_str}"
        super().__init__(message)


class ConfigurationError(ContainerError):
    """Raised when container configuration is invalid.

    This can occur when:
    - Required environment variables are missing
    - Configuration values are invalid
    - Incompatible settings are specified
    """

    def __init__(self, message: str, config_key: str | None = None):
        super().__init__(message)
        self.config_key = config_key
