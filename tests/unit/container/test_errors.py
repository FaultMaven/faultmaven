"""Tests for container errors.

Tests cover:
- Exception hierarchy
- Error attributes
- Error messages
"""

import pytest

from faultmaven.container.errors import (
    CircularDependencyError,
    ConfigurationError,
    ContainerError,
    InitializationError,
    ServiceUnavailableError,
)


class TestContainerError:
    """Test base ContainerError."""

    def test_container_error_message(self):
        """Test ContainerError with message."""
        error = ContainerError("Something went wrong")

        assert str(error) == "Something went wrong"
        assert error.service_name is None

    def test_container_error_with_service(self):
        """Test ContainerError with service name."""
        error = ContainerError("Failed", service_name="test_service")

        assert error.service_name == "test_service"


class TestServiceUnavailableError:
    """Test ServiceUnavailableError."""

    def test_basic_error(self):
        """Test error with just service name."""
        error = ServiceUnavailableError("llm_provider")

        assert "llm_provider" in str(error)
        assert error.service_name == "llm_provider"
        assert error.reason is None

    def test_error_with_reason(self):
        """Test error with reason."""
        error = ServiceUnavailableError("redis", reason="Connection refused")

        assert "redis" in str(error)
        assert "Connection refused" in str(error)
        assert error.reason == "Connection refused"

    def test_error_is_container_error(self):
        """Test inheritance from ContainerError."""
        error = ServiceUnavailableError("test")

        assert isinstance(error, ContainerError)


class TestInitializationError:
    """Test InitializationError."""

    def test_basic_error(self):
        """Test error with message."""
        error = InitializationError("Failed to start")

        assert str(error) == "Failed to start"
        assert error.cause is None

    def test_error_with_service(self):
        """Test error with service name."""
        error = InitializationError("Config invalid", service_name="config")

        assert error.service_name == "config"

    def test_error_with_cause(self):
        """Test error with cause exception."""
        cause = ValueError("Bad value")
        error = InitializationError("Init failed", cause=cause)

        assert error.cause is cause
        assert error.__cause__ is cause

    def test_error_is_container_error(self):
        """Test inheritance from ContainerError."""
        error = InitializationError("test")

        assert isinstance(error, ContainerError)


class TestCircularDependencyError:
    """Test CircularDependencyError."""

    def test_basic_cycle(self):
        """Test error with dependency chain."""
        error = CircularDependencyError(["a", "b", "c", "a"])

        assert "a -> b -> c -> a" in str(error)
        assert error.dependency_chain == ["a", "b", "c", "a"]

    def test_two_element_cycle(self):
        """Test error with two-element cycle."""
        error = CircularDependencyError(["x", "y", "x"])

        assert "x -> y -> x" in str(error)

    def test_error_is_container_error(self):
        """Test inheritance from ContainerError."""
        error = CircularDependencyError(["a", "b"])

        assert isinstance(error, ContainerError)


class TestConfigurationError:
    """Test ConfigurationError."""

    def test_basic_error(self):
        """Test error with message."""
        error = ConfigurationError("Invalid config")

        assert str(error) == "Invalid config"
        assert error.config_key is None

    def test_error_with_key(self):
        """Test error with config key."""
        error = ConfigurationError("Missing value", config_key="DATABASE_URL")

        assert error.config_key == "DATABASE_URL"

    def test_error_is_container_error(self):
        """Test inheritance from ContainerError."""
        error = ConfigurationError("test")

        assert isinstance(error, ContainerError)
