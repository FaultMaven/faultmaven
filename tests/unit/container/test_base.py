"""Tests for BaseDIContainer.

Tests cover:
- Singleton pattern
- Service registration via registry
- Service access patterns
- Health check
- Reset functionality
"""

import pytest

from faultmaven.container.base import BaseDIContainer
from faultmaven.container.errors import ServiceUnavailableError
from faultmaven.container.registry import ServiceStatus


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton before and after each test."""
    BaseDIContainer.reset_singleton()
    yield
    BaseDIContainer.reset_singleton()


class TestBaseDIContainerSingleton:
    """Test singleton behavior."""

    def test_singleton_returns_same_instance(self):
        """Test that multiple calls return same instance."""
        container1 = BaseDIContainer()
        container2 = BaseDIContainer()

        assert container1 is container2

    def test_reset_singleton_creates_new_instance(self):
        """Test that reset_singleton allows new instance."""
        container1 = BaseDIContainer()
        BaseDIContainer.reset_singleton()
        container2 = BaseDIContainer()

        assert container1 is not container2


class TestServiceRegistration:
    """Test service registration methods."""

    def test_register_service(self):
        """Test registering a service."""
        container = BaseDIContainer()

        container._register_service("test", "test_value")

        assert container.get_service("test") == "test_value"
        assert hasattr(container, "test")
        assert container.test == "test_value"

    def test_register_service_with_dependencies(self):
        """Test registering a service with dependencies."""
        container = BaseDIContainer()

        container._register_service("parent", "parent_value")
        container._register_service("child", "child_value", dependencies=["parent"])

        info = container.registry.get_info("child")
        assert info.dependencies == ["parent"]

    def test_register_failed(self):
        """Test registering a failed service."""
        container = BaseDIContainer()

        container._register_failed("broken", "Connection refused")

        assert container.get_service("broken") is None
        info = container.registry.get_info("broken")
        assert info.status == ServiceStatus.FAILED
        assert info.error == "Connection refused"

    def test_register_disabled(self):
        """Test registering a disabled service."""
        container = BaseDIContainer()

        container._register_disabled("optional", "Not needed in dev")

        assert container.get_service("optional") is None
        info = container.registry.get_info("optional")
        assert info.status == ServiceStatus.DISABLED


class TestServiceAccess:
    """Test service access methods."""

    def test_get_service_returns_instance(self):
        """Test get_service returns registered instance."""
        container = BaseDIContainer()
        container._register_service("test", "value")

        assert container.get_service("test") == "value"

    def test_get_service_returns_none_for_missing(self):
        """Test get_service returns None for missing service."""
        container = BaseDIContainer()

        assert container.get_service("missing") is None

    def test_get_service_required_raises(self):
        """Test get_service with required=True raises for missing."""
        container = BaseDIContainer()

        with pytest.raises(ServiceUnavailableError):
            container.get_service("missing", required=True)

    def test_get_service_required_returns_instance(self):
        """Test get_service with required=True returns instance."""
        container = BaseDIContainer()
        container._register_service("test", "value")

        assert container.get_service("test", required=True) == "value"

    def test_has_service(self):
        """Test has_service method."""
        container = BaseDIContainer()

        assert not container.has_service("test")

        container._register_service("test", "value")
        assert container.has_service("test")

    def test_get_service_status(self):
        """Test get_service_status method."""
        container = BaseDIContainer()

        assert container.get_service_status("test") is None

        container._register_service("test", "value")
        assert container.get_service_status("test") == ServiceStatus.READY

        container._register_failed("broken", "error")
        assert container.get_service_status("broken") == ServiceStatus.FAILED


class TestHealth:
    """Test health check functionality."""

    def test_get_health_empty(self):
        """Test get_health with no services."""
        container = BaseDIContainer()

        health = container.get_health()

        assert health["status"] == "not_initialized"
        assert health["services"]["total"] == 0

    def test_get_health_with_ready_services(self):
        """Test get_health with ready services."""
        container = BaseDIContainer()
        container._register_service("a", "value_a")
        container._register_service("b", "value_b")

        health = container.get_health()

        assert health["status"] == "healthy"
        assert health["services"]["total"] == 2
        assert health["services"]["ready"] == 2
        assert health["services"]["failed"] == 0

    def test_get_health_with_failed_services(self):
        """Test get_health with failed services."""
        container = BaseDIContainer()
        container._register_service("working", "value")
        container._register_failed("broken", "error")

        health = container.get_health()

        assert health["status"] == "degraded"
        assert health["services"]["failed"] == 1
        assert "broken" in health["failed_services"]


class TestReset:
    """Test reset functionality."""

    def test_reset_clears_services(self):
        """Test that reset clears all registered services."""
        container = BaseDIContainer()
        container._register_service("test", "value")

        assert container.has_service("test")

        container.reset()

        assert not container.has_service("test")
        assert not hasattr(container, "test")

    def test_reset_clears_initialization_state(self):
        """Test that reset clears initialization state."""
        container = BaseDIContainer()
        container._initialized = True
        container._initializing = True

        container.reset()

        assert not container._initialized
        assert not container._initializing
