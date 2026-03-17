"""Tests for DependencyRegistry.

Tests cover:
- Service registration
- Instance management
- Dependency tracking
- Circular dependency detection
- Initialization order
"""

from datetime import datetime, timezone

import pytest

from faultmaven.container.errors import CircularDependencyError, ServiceUnavailableError
from faultmaven.container.registry import (
    DependencyError,
    DependencyRegistry,
    ServiceInfo,
    ServiceStatus,
)


class TestServiceInfo:
    """Test ServiceInfo dataclass."""

    def test_service_info_creation(self):
        """Test creating ServiceInfo with defaults."""
        info = ServiceInfo(name="test_service")

        assert info.name == "test_service"
        assert info.status == ServiceStatus.PENDING
        assert info.initialized_at is None
        assert info.error is None
        assert info.dependencies == []
        assert info.instance is None

    def test_service_info_is_available(self):
        """Test is_available method."""
        info = ServiceInfo(name="test")
        assert not info.is_available()

        info.status = ServiceStatus.READY
        assert not info.is_available()  # Still no instance

        info.instance = object()
        assert info.is_available()

    def test_service_info_with_dependencies(self):
        """Test ServiceInfo with dependencies."""
        info = ServiceInfo(
            name="dependent",
            dependencies=["dep1", "dep2"],
        )

        assert info.dependencies == ["dep1", "dep2"]


class TestDependencyRegistry:
    """Test DependencyRegistry functionality."""

    def test_register_service(self):
        """Test registering a service."""
        registry = DependencyRegistry()

        info = registry.register("test_service")

        assert info.name == "test_service"
        assert "test_service" in registry
        assert len(registry) == 1

    def test_register_with_dependencies(self):
        """Test registering a service with dependencies."""
        registry = DependencyRegistry()

        info = registry.register("child", dependencies=["parent1", "parent2"])

        assert info.dependencies == ["parent1", "parent2"]

    def test_register_duplicate_returns_existing(self):
        """Test that registering duplicate returns existing info."""
        registry = DependencyRegistry()

        info1 = registry.register("test")
        info2 = registry.register("test")

        assert info1 is info2

    def test_set_instance(self):
        """Test setting a service instance."""
        registry = DependencyRegistry()
        registry.register("test")

        instance = object()
        registry.set_instance("test", instance)

        info = registry.get_info("test")
        assert info.instance is instance
        assert info.status == ServiceStatus.READY
        assert info.initialized_at is not None

    def test_set_instance_auto_registers(self):
        """Test that set_instance auto-registers if not registered."""
        registry = DependencyRegistry()

        instance = object()
        registry.set_instance("auto", instance)

        assert "auto" in registry
        assert registry.get("auto") is instance

    def test_set_failed(self):
        """Test marking a service as failed."""
        registry = DependencyRegistry()
        registry.register("test")

        registry.set_failed("test", "Connection refused")

        info = registry.get_info("test")
        assert info.status == ServiceStatus.FAILED
        assert info.error == "Connection refused"

    def test_set_disabled(self):
        """Test marking a service as disabled."""
        registry = DependencyRegistry()
        registry.register("test")

        registry.set_disabled("test", "Not needed in dev")

        info = registry.get_info("test")
        assert info.status == ServiceStatus.DISABLED
        assert info.error == "Not needed in dev"

    def test_get_returns_none_for_unavailable(self):
        """Test get returns None for unavailable services."""
        registry = DependencyRegistry()
        registry.register("test")

        assert registry.get("test") is None
        assert registry.get("nonexistent") is None

    def test_get_returns_instance_when_ready(self):
        """Test get returns instance when service is ready."""
        registry = DependencyRegistry()
        instance = object()
        registry.set_instance("test", instance)

        assert registry.get("test") is instance

    def test_get_required_raises_for_unavailable(self):
        """Test get_required raises for unavailable services."""
        registry = DependencyRegistry()
        registry.register("test")
        registry.set_failed("test", "Init failed")

        with pytest.raises(ServiceUnavailableError) as exc:
            registry.get_required("test")

        assert exc.value.service_name == "test"
        assert "Init failed" in exc.value.reason

    def test_get_required_returns_instance(self):
        """Test get_required returns instance when available."""
        registry = DependencyRegistry()
        instance = object()
        registry.set_instance("test", instance)

        assert registry.get_required("test") is instance


class TestDependencyResolution:
    """Test dependency resolution and ordering."""

    def test_initialization_order_simple(self):
        """Test initialization order with simple dependencies."""
        registry = DependencyRegistry()
        registry.register("parent")
        registry.register("child", dependencies=["parent"])

        order = registry.get_initialization_order()

        assert order.index("parent") < order.index("child")

    def test_initialization_order_complex(self):
        """Test initialization order with multiple dependencies."""
        registry = DependencyRegistry()
        registry.register("db")
        registry.register("cache")
        registry.register("repo", dependencies=["db"])
        registry.register("service", dependencies=["repo", "cache"])

        order = registry.get_initialization_order()

        assert order.index("db") < order.index("repo")
        assert order.index("repo") < order.index("service")
        assert order.index("cache") < order.index("service")

    def test_circular_dependency_detection(self):
        """Test that circular dependencies are detected."""
        registry = DependencyRegistry()
        registry.register("a", dependencies=["b"])
        registry.register("b", dependencies=["c"])
        registry.register("c", dependencies=["a"])

        with pytest.raises(CircularDependencyError) as exc:
            registry.get_initialization_order()

        assert "a" in exc.value.dependency_chain
        assert "b" in exc.value.dependency_chain
        assert "c" in exc.value.dependency_chain

    def test_self_circular_dependency(self):
        """Test that self-referential dependency is detected."""
        registry = DependencyRegistry()
        registry.register("self_ref", dependencies=["self_ref"])

        with pytest.raises(CircularDependencyError):
            registry.get_initialization_order()

    def test_validate_dependencies_missing(self):
        """Test validation detects missing dependencies."""
        registry = DependencyRegistry()
        registry.register("service", dependencies=["missing1", "missing2"])

        missing = registry.validate_dependencies()

        assert "missing1" in missing
        assert "missing2" in missing

    def test_validate_dependencies_all_present(self):
        """Test validation passes when all deps present."""
        registry = DependencyRegistry()
        registry.register("dep1")
        registry.register("dep2")
        registry.register("service", dependencies=["dep1", "dep2"])

        missing = registry.validate_dependencies()

        assert missing == []


class TestRegistryQueries:
    """Test registry query methods."""

    def test_get_all_services(self):
        """Test getting all registered services."""
        registry = DependencyRegistry()
        registry.register("a")
        registry.register("b")

        all_services = registry.get_all_services()

        assert "a" in all_services
        assert "b" in all_services
        assert len(all_services) == 2

    def test_get_ready_services(self):
        """Test getting ready services."""
        registry = DependencyRegistry()
        registry.set_instance("ready1", object())
        registry.set_instance("ready2", object())
        registry.register("pending")
        registry.set_failed("failed", "error")

        ready = registry.get_ready_services()

        assert "ready1" in ready
        assert "ready2" in ready
        assert "pending" not in ready
        assert "failed" not in ready

    def test_get_failed_services(self):
        """Test getting failed services."""
        registry = DependencyRegistry()
        registry.set_instance("ready", object())
        registry.set_failed("failed1", "error1")
        registry.set_failed("failed2", "error2")

        failed = registry.get_failed_services()

        assert ("failed1", "error1") in failed
        assert ("failed2", "error2") in failed
        assert len(failed) == 2

    def test_clear_registry(self):
        """Test clearing the registry."""
        registry = DependencyRegistry()
        registry.register("a")
        registry.register("b")

        registry.clear()

        assert len(registry) == 0
        assert "a" not in registry
