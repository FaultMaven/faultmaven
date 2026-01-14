"""Unit tests for Dependency Injection Container.

Tests cover:
- Service registration (factory and instance)
- Singleton behavior
- Lazy initialization
- Error handling (missing factories)
- Test isolation (clear methods)
- Thread safety considerations
- Status checking methods

Design Reference: Phase 3, Week 14-15 - DI Container Implementation
"""

import pytest
from unittest.mock import Mock, MagicMock, call
from faultmaven.core.container import (
    ServiceContainer,
    ServiceContainerError,
    ServiceNotFoundError,
    CircularDependencyError,
)


# ============================================================================
# Test Fixtures
# ============================================================================


class MockServiceA:
    """Mock service for testing."""

    def __init__(self, value: str = "A"):
        self.value = value
        self.initialized = True


class MockServiceB:
    """Another mock service for testing."""

    def __init__(self, value: str = "B"):
        self.value = value
        self.initialized = True


class MockServiceWithDependency:
    """Mock service that depends on another service."""

    def __init__(self, dependency: MockServiceA):
        self.dependency = dependency
        self.initialized = True


@pytest.fixture(autouse=True)
def clean_container():
    """Clean the container before and after each test."""
    ServiceContainer.clear_all()
    yield
    ServiceContainer.clear_all()


# ============================================================================
# Test Service Registration
# ============================================================================


class TestServiceRegistration:
    """Test service registration functionality."""

    def test_register_factory_simple(self):
        """Test registering a simple factory."""
        factory = lambda: MockServiceA("test")
        ServiceContainer.register_factory(MockServiceA, factory)

        assert ServiceContainer.has_factory(MockServiceA)
        assert not ServiceContainer.has_instance(MockServiceA)

    def test_register_factory_multiple_services(self):
        """Test registering multiple different services."""
        factory_a = lambda: MockServiceA("a")
        factory_b = lambda: MockServiceB("b")

        ServiceContainer.register_factory(MockServiceA, factory_a)
        ServiceContainer.register_factory(MockServiceB, factory_b)

        assert ServiceContainer.has_factory(MockServiceA)
        assert ServiceContainer.has_factory(MockServiceB)
        assert len(ServiceContainer._factories) == 2

    def test_register_instance_directly(self):
        """Test registering a pre-created instance."""
        instance = MockServiceA("pre-created")
        ServiceContainer.register_instance(MockServiceA, instance)

        assert ServiceContainer.has_instance(MockServiceA)
        retrieved = ServiceContainer.get(MockServiceA)
        assert retrieved is instance
        assert retrieved.value == "pre-created"

    def test_register_factory_overwrite(self):
        """Test that re-registering a factory overwrites the previous one."""
        factory1 = lambda: MockServiceA("first")
        factory2 = lambda: MockServiceA("second")

        ServiceContainer.register_factory(MockServiceA, factory1)
        ServiceContainer.register_factory(MockServiceA, factory2)

        # Should use the second factory
        instance = ServiceContainer.get(MockServiceA)
        assert instance.value == "second"


# ============================================================================
# Test Service Retrieval
# ============================================================================


class TestServiceRetrieval:
    """Test service retrieval and singleton behavior."""

    def test_get_service_creates_instance(self):
        """Test that get() creates instance from factory."""
        factory = lambda: MockServiceA("created")
        ServiceContainer.register_factory(MockServiceA, factory)

        instance = ServiceContainer.get(MockServiceA)

        assert isinstance(instance, MockServiceA)
        assert instance.value == "created"
        assert instance.initialized

    def test_get_service_singleton_behavior(self):
        """Test that get() returns the same instance on multiple calls."""
        call_count = 0

        def counting_factory():
            nonlocal call_count
            call_count += 1
            return MockServiceA(f"call_{call_count}")

        ServiceContainer.register_factory(MockServiceA, counting_factory)

        instance1 = ServiceContainer.get(MockServiceA)
        instance2 = ServiceContainer.get(MockServiceA)
        instance3 = ServiceContainer.get(MockServiceA)

        # Should only create once
        assert call_count == 1
        assert instance1 is instance2
        assert instance2 is instance3
        assert instance1.value == "call_1"

    def test_get_service_not_registered(self):
        """Test that get() raises ValueError for unregistered service."""
        with pytest.raises(ValueError) as exc_info:
            ServiceContainer.get(MockServiceA)

        assert "No factory registered" in str(exc_info.value)
        assert "MockServiceA" in str(exc_info.value)

    def test_get_multiple_different_services(self):
        """Test getting multiple different service types."""
        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA("a"))
        ServiceContainer.register_factory(MockServiceB, lambda: MockServiceB("b"))

        service_a = ServiceContainer.get(MockServiceA)
        service_b = ServiceContainer.get(MockServiceB)

        assert isinstance(service_a, MockServiceA)
        assert isinstance(service_b, MockServiceB)
        assert service_a.value == "a"
        assert service_b.value == "b"

    def test_get_service_with_factory_error(self):
        """Test that factory errors are propagated correctly."""

        def failing_factory():
            raise RuntimeError("Factory initialization failed")

        ServiceContainer.register_factory(MockServiceA, failing_factory)

        with pytest.raises(RuntimeError) as exc_info:
            ServiceContainer.get(MockServiceA)

        assert "Factory initialization failed" in str(exc_info.value)


# ============================================================================
# Test Container State Management
# ============================================================================


class TestContainerStateManagement:
    """Test container state management (clear, status)."""

    def test_clear_instances_only(self):
        """Test that clear() removes instances but keeps factories."""
        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA("test"))

        # Create instance
        instance1 = ServiceContainer.get(MockServiceA)
        assert ServiceContainer.has_instance(MockServiceA)

        # Clear instances
        ServiceContainer.clear()

        # Factory should still be registered
        assert ServiceContainer.has_factory(MockServiceA)
        assert not ServiceContainer.has_instance(MockServiceA)

        # Should create new instance on next get()
        instance2 = ServiceContainer.get(MockServiceA)
        assert instance2 is not instance1

    def test_clear_all_removes_everything(self):
        """Test that clear_all() removes instances AND factories."""
        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA("test"))
        ServiceContainer.get(MockServiceA)

        assert ServiceContainer.has_factory(MockServiceA)
        assert ServiceContainer.has_instance(MockServiceA)

        ServiceContainer.clear_all()

        assert not ServiceContainer.has_factory(MockServiceA)
        assert not ServiceContainer.has_instance(MockServiceA)

    def test_has_factory_check(self):
        """Test has_factory() returns correct status."""
        assert not ServiceContainer.has_factory(MockServiceA)

        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA())

        assert ServiceContainer.has_factory(MockServiceA)

    def test_has_instance_check(self):
        """Test has_instance() returns correct status."""
        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA())

        assert not ServiceContainer.has_instance(MockServiceA)

        ServiceContainer.get(MockServiceA)

        assert ServiceContainer.has_instance(MockServiceA)

    def test_get_registered_services_empty(self):
        """Test get_registered_services() with no services."""
        status = ServiceContainer.get_registered_services()

        assert status == {}

    def test_get_registered_services_with_services(self):
        """Test get_registered_services() with registered services."""
        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA())
        ServiceContainer.register_factory(MockServiceB, lambda: MockServiceB())

        # Create only ServiceA instance
        ServiceContainer.get(MockServiceA)

        status = ServiceContainer.get_registered_services()

        assert status == {
            "MockServiceA": True,  # Has instance
            "MockServiceB": False,  # No instance yet
        }


# ============================================================================
# Test Dependency Injection Patterns
# ============================================================================


class TestDependencyInjection:
    """Test dependency injection patterns."""

    def test_inject_dependency_via_container(self):
        """Test injecting one service into another via container."""
        # Register dependency
        ServiceContainer.register_factory(
            MockServiceA, lambda: MockServiceA("dependency")
        )

        # Register service that needs dependency
        def create_dependent_service():
            dependency = ServiceContainer.get(MockServiceA)
            return MockServiceWithDependency(dependency)

        ServiceContainer.register_factory(
            MockServiceWithDependency, create_dependent_service
        )

        # Get service
        service = ServiceContainer.get(MockServiceWithDependency)

        assert isinstance(service, MockServiceWithDependency)
        assert isinstance(service.dependency, MockServiceA)
        assert service.dependency.value == "dependency"

    def test_optional_dependency_injection(self):
        """Test optional dependency injection pattern (for backward compatibility)."""
        # This pattern is used in refactored services:
        # def __init__(self, embedding_service=None):
        #     self.embedding_service = embedding_service or ServiceContainer.get(EmbeddingService)

        ServiceContainer.register_factory(
            MockServiceA, lambda: MockServiceA("from-container")
        )

        # Case 1: Dependency provided directly (testing, manual wiring)
        manual_dep = MockServiceA("manual")
        result1 = manual_dep if manual_dep else ServiceContainer.get(MockServiceA)
        assert result1.value == "manual"

        # Case 2: Dependency not provided, use container
        result2 = None or ServiceContainer.get(MockServiceA)
        assert result2.value == "from-container"

    def test_mock_service_for_testing(self):
        """Test replacing service with mock for testing."""
        # Production factory
        ServiceContainer.register_factory(
            MockServiceA, lambda: MockServiceA("production")
        )

        # Replace with mock for testing
        mock_service = Mock(spec=MockServiceA)
        mock_service.value = "mocked"
        ServiceContainer.register_instance(MockServiceA, mock_service)

        # Should get mock
        service = ServiceContainer.get(MockServiceA)
        assert service is mock_service
        assert service.value == "mocked"


# ============================================================================
# Test Error Handling
# ============================================================================


class TestErrorHandling:
    """Test error handling and edge cases."""

    def test_get_unregistered_service_error_message(self):
        """Test that error message for unregistered service is helpful."""
        with pytest.raises(ValueError) as exc_info:
            ServiceContainer.get(MockServiceA)

        error_message = str(exc_info.value)
        assert "No factory registered" in error_message
        assert "MockServiceA" in error_message
        assert "register_factory" in error_message

    def test_factory_exception_propagates(self):
        """Test that exceptions in factory are propagated with context."""

        def bad_factory():
            raise ValueError("Configuration error")

        ServiceContainer.register_factory(MockServiceA, bad_factory)

        with pytest.raises(ValueError) as exc_info:
            ServiceContainer.get(MockServiceA)

        assert "Configuration error" in str(exc_info.value)

    def test_clear_empty_container(self):
        """Test that clearing empty container doesn't raise errors."""
        ServiceContainer.clear()
        ServiceContainer.clear_all()

        # Should not raise any errors
        assert True


# ============================================================================
# Test Integration Scenarios
# ============================================================================


class TestIntegrationScenarios:
    """Test realistic integration scenarios."""

    def test_complete_service_lifecycle(self):
        """Test complete lifecycle: register -> get -> use -> clear -> re-get."""
        call_count = 0

        def factory():
            nonlocal call_count
            call_count += 1
            return MockServiceA(f"instance_{call_count}")

        # 1. Register
        ServiceContainer.register_factory(MockServiceA, factory)
        assert call_count == 0

        # 2. Get (creates instance)
        instance1 = ServiceContainer.get(MockServiceA)
        assert call_count == 1
        assert instance1.value == "instance_1"

        # 3. Get again (returns same instance)
        instance1_again = ServiceContainer.get(MockServiceA)
        assert call_count == 1
        assert instance1_again is instance1

        # 4. Clear instances
        ServiceContainer.clear()
        assert call_count == 1

        # 5. Get after clear (creates new instance)
        instance2 = ServiceContainer.get(MockServiceA)
        assert call_count == 2
        assert instance2.value == "instance_2"
        assert instance2 is not instance1

    def test_multiple_services_independent(self):
        """Test that multiple services are independently managed."""
        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA("A"))
        ServiceContainer.register_factory(MockServiceB, lambda: MockServiceB("B"))

        # Create ServiceA
        service_a = ServiceContainer.get(MockServiceA)
        assert ServiceContainer.has_instance(MockServiceA)
        assert not ServiceContainer.has_instance(MockServiceB)

        # Create ServiceB
        service_b = ServiceContainer.get(MockServiceB)
        assert ServiceContainer.has_instance(MockServiceA)
        assert ServiceContainer.has_instance(MockServiceB)

        # Clear instances
        ServiceContainer.clear()
        assert not ServiceContainer.has_instance(MockServiceA)
        assert not ServiceContainer.has_instance(MockServiceB)

        # Factories still registered
        assert ServiceContainer.has_factory(MockServiceA)
        assert ServiceContainer.has_factory(MockServiceB)

    def test_service_chain_dependency(self):
        """Test service that depends on another service (realistic DI scenario)."""
        # Setup: ServiceC -> ServiceB -> ServiceA

        class ServiceA:
            def __init__(self):
                self.name = "A"

        class ServiceB:
            def __init__(self):
                self.name = "B"
                self.service_a = ServiceContainer.get(ServiceA)

        class ServiceC:
            def __init__(self):
                self.name = "C"
                self.service_b = ServiceContainer.get(ServiceB)

        # Register factories
        ServiceContainer.register_factory(ServiceA, lambda: ServiceA())
        ServiceContainer.register_factory(ServiceB, lambda: ServiceB())
        ServiceContainer.register_factory(ServiceC, lambda: ServiceC())

        # Get top-level service
        service_c = ServiceContainer.get(ServiceC)

        # Verify chain
        assert service_c.name == "C"
        assert service_c.service_b.name == "B"
        assert service_c.service_b.service_a.name == "A"

        # All should be singletons
        assert ServiceContainer.get(ServiceA) is service_c.service_b.service_a
        assert ServiceContainer.get(ServiceB) is service_c.service_b


# ============================================================================
# Test Thread Safety (Basic)
# ============================================================================


class TestThreadSafety:
    """Basic thread safety tests (container has simple lock mechanism)."""

    def test_concurrent_get_returns_same_instance(self):
        """Test that concurrent gets return the same singleton instance."""
        import threading

        ServiceContainer.register_factory(MockServiceA, lambda: MockServiceA("shared"))

        instances = []

        def get_service():
            instances.append(ServiceContainer.get(MockServiceA))

        # Create 10 threads that all get the service
        threads = [threading.Thread(target=get_service) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should be the same instance
        assert len(instances) == 10
        assert all(instance is instances[0] for instance in instances)


# ============================================================================
# Test Custom Exception Classes
# ============================================================================


class TestCustomExceptions:
    """Test custom exception classes."""

    def test_service_not_found_error(self):
        """Test ServiceNotFoundError exception."""
        error = ServiceNotFoundError(MockServiceA)

        assert isinstance(error, ServiceContainerError)
        assert error.service_type is MockServiceA
        assert "MockServiceA" in str(error)
        assert "not registered" in str(error)

    def test_circular_dependency_error(self):
        """Test CircularDependencyError exception."""
        chain = [MockServiceA, MockServiceB, MockServiceA]
        error = CircularDependencyError(chain)

        assert isinstance(error, ServiceContainerError)
        assert error.service_chain == chain
        assert "Circular dependency" in str(error)
        assert "MockServiceA -> MockServiceB -> MockServiceA" in str(error)
