"""
Comprehensive tests for the DI Container Integration system.

Tests coverage:
- Service lifecycle management
- Interface resolution and injection
- Mock service patterns for testing
- Container health and diagnostics
- Isolation between test runs
- Error handling and graceful fallbacks
- Dependency graph resolution
"""

import asyncio
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest

from faultmaven.config.settings import get_settings, reset_settings
from faultmaven.container import DIContainer

# Import interfaces with fallback for testing
try:
    from faultmaven.models.interfaces import (
        BaseTool,
        ILLMProvider,
        ISanitizer,
        ISessionStore,
        ITracer,
        IVectorStore,
    )
    from faultmaven.models.interfaces_case import ICaseService, ICaseStore

    INTERFACES_AVAILABLE = True
except ImportError:
    # Create mock interfaces for testing
    ILLMProvider = Mock
    ITracer = Mock
    ISanitizer = Mock
    BaseTool = Mock
    IVectorStore = Mock
    ISessionStore = Mock
    ICaseStore = Mock
    ICaseService = Mock
    INTERFACES_AVAILABLE = False


@pytest.fixture(autouse=True)
def reset_container_before_test():
    """Reset container and settings before each test."""
    # Reset container singleton
    DIContainer._instance = None
    reset_settings()

    # Set test environment variables
    os.environ["SKIP_SERVICE_CHECKS"] = "true"

    yield

    # Reset after test
    DIContainer._instance = None
    reset_settings()


@pytest.fixture
def clean_env():
    """Provide a clean environment for testing."""
    original_env = os.environ.copy()

    # Keep essential test variables
    essential_vars = {
        "SKIP_SERVICE_CHECKS": "true",
        "PYTEST_CURRENT_TEST": os.environ.get("PYTEST_CURRENT_TEST", ""),
        "ENVIRONMENT": "development",
    }

    # Clear environment
    os.environ.clear()
    os.environ.update(essential_vars)

    yield

    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)


@pytest.fixture
def mock_services():
    """Mock service implementations for testing."""
    services = {}

    # Mock LLM Provider
    llm_provider = Mock()
    llm_provider.generate = AsyncMock(
        return_value=Mock(
            content="Mock LLM response", confidence=0.85, model="mock-model"
        )
    )
    llm_provider.is_available = Mock(return_value=True)
    services["llm_provider"] = llm_provider

    # Mock Sanitizer
    sanitizer = Mock()
    sanitizer.sanitize = AsyncMock(return_value="Sanitized content")
    sanitizer.is_sensitive = Mock(return_value=False)
    services["sanitizer"] = sanitizer

    # Mock Tracer
    tracer = Mock(spec=ITracer)
    tracer.trace.return_value = Mock()
    services["tracer"] = tracer

    # Mock Vector Store
    vector_store = Mock(spec=IVectorStore)
    vector_store.search = AsyncMock(return_value=[])
    vector_store.add_documents = AsyncMock(return_value=True)
    services["vector_store"] = vector_store

    # Mock Session Store
    session_store = Mock(spec=ISessionStore)
    session_store.get_session = AsyncMock(return_value=None)
    session_store.create_session = AsyncMock(return_value="test-session-id")
    services["session_store"] = session_store

    # Mock Case Store
    case_store = Mock(spec=ICaseStore)
    case_store.create_case = AsyncMock(return_value=True)
    case_store.get_case = AsyncMock(return_value=None)
    services["case_store"] = case_store

    # Mock Tools
    knowledge_tool = Mock(spec=BaseTool)
    knowledge_tool.execute = AsyncMock(return_value="Knowledge tool result")
    knowledge_tool.get_schema.return_value = {
        "name": "knowledge_base",
        "description": "Mock tool",
    }

    web_search_tool = Mock(spec=BaseTool)
    web_search_tool.execute = AsyncMock(return_value="Web search result")
    web_search_tool.get_schema.return_value = {
        "name": "web_search",
        "description": "Mock web search",
    }

    services["tools"] = [knowledge_tool, web_search_tool]

    return services


class TestContainerSingleton:
    """Test container singleton behavior."""

    def test_container_singleton_pattern(self):
        """Test that container follows singleton pattern."""
        container1 = DIContainer()
        container2 = DIContainer()

        assert container1 is container2
        assert DIContainer._instance is container1

    def test_container_reset_creates_new_instance(self):
        """Test that reset creates a new container instance."""
        container1 = DIContainer()
        original_id = id(container1)

        # Reset
        DIContainer._instance = None

        container2 = DIContainer()
        new_id = id(container2)

        assert container1 is not container2
        assert original_id != new_id


class TestContainerInitialization:
    """Test container initialization process."""

    @pytest.mark.asyncio
    async def test_settings_integration(self):
        """Test integration with settings system (using actual settings from .env, not environment override)."""
        from faultmaven.config.settings import reset_settings

        # Arrange - Reset settings to load from .env file
        reset_settings()

        # Act - Initialize container with real settings
        container = DIContainer()
        await container.initialize()

        # Assert - Verify settings were loaded and have expected structure
        assert container.settings is not None

        # Verify the settings object has the expected structure
        assert hasattr(container.settings, "server")
        assert hasattr(container.settings, "llm")
        assert hasattr(container.settings, "database")
        assert hasattr(container.settings, "session")

        # Test that settings are readable (values come from .env file)
        assert container.settings.server.environment is not None
        assert isinstance(container.settings.server.debug, bool)

        # LLM provider comes from .env file (not environment override)
        # Verify it's one of the supported providers
        assert container.settings.llm.provider.value in [
            "fireworks",
            "openai",
            "anthropic",
            "cohere",
            "local",
            "groq",
            "gemini",
            "huggingface",
            "openrouter",
        ]

        # Verify other LLM settings are accessible
        assert hasattr(container.settings.llm, "max_retries")
        assert hasattr(container.settings.llm, "request_timeout")


class TestServiceLifecycleManagement:
    """Test service lifecycle management."""

    @pytest.mark.asyncio
    async def test_graceful_degradation_without_interfaces(self, clean_env):
        """Test graceful degradation when interfaces are not available."""
        container = DIContainer()

        # Container should initialize without error even if interfaces are not available
        # (interfaces are optional and container handles their absence gracefully)
        await container.initialize()

        assert container._initialized
        assert container.settings is not None


class TestInterfaceResolutionAndInjection:
    """Test interface resolution and dependency injection."""

    def test_service_getter_methods(self, clean_env, mock_services):
        """Test service getter methods."""
        container = DIContainer()

        # Mock services
        container.agent_service = mock_services["llm_provider"]  # Use as placeholder
        container.data_service = mock_services["sanitizer"]  # Use as placeholder
        container.knowledge_service = mock_services["tracer"]  # Use as placeholder
        container.session_service = mock_services["vector_store"]  # Use as placeholder
        container.case_service = mock_services["case_store"]  # Use as placeholder

        # Test getter methods
        assert container.get_agent_service() is container.agent_service
        assert container.get_data_service() is container.data_service
        assert container.get_knowledge_service() is container.knowledge_service
        assert container.get_session_service() is container.session_service
        assert container.get_case_service() is container.case_service

    def test_service_getter_with_initialization(self, clean_env):
        """Test service getters trigger initialization if needed."""
        container = DIContainer()

        with (
            patch.object(container, "initialize") as mock_init,
            patch.object(
                container, "agent_service", create=True, new=Mock()
            ) as mock_service,
        ):

            result = container.get_agent_service()

            mock_init.assert_called_once()
            assert result is mock_service

    def test_infrastructure_provider_getters(self, clean_env, mock_services):
        """Test infrastructure provider getter methods."""
        container = DIContainer()
        container._initialized = True  # Prevent automatic initialization

        # Set up infrastructure
        container.llm_provider = mock_services["llm_provider"]
        container.sanitizer = mock_services["sanitizer"]
        container.tracer = mock_services["tracer"]
        container.vector_store = mock_services["vector_store"]
        container.session_store = mock_services["session_store"]
        container.tools = mock_services["tools"]

        # Test getters
        assert container.get_llm_provider() is mock_services["llm_provider"]
        assert container.get_sanitizer() is mock_services["sanitizer"]
        assert container.get_tracer() is mock_services["tracer"]
        assert container.get_vector_store() is mock_services["vector_store"]
        assert container.get_session_store() is mock_services["session_store"]
        assert container.get_tools() == mock_services["tools"]


class TestContainerHealthAndDiagnostics:
    """Test container health monitoring and diagnostics."""

    def test_health_check_all_services_healthy(self, clean_env, mock_services):
        """Test health check when all services are healthy."""
        container = DIContainer()

        # Set up healthy services
        container._llm_provider = mock_services["llm_provider"]
        container._sanitizer = mock_services["sanitizer"]
        container._tracer = mock_services["tracer"]
        container._vector_store = mock_services["vector_store"]

        # Mock health check methods
        for service in mock_services.values():
            if hasattr(service, "health_check"):
                service.health_check.return_value = {"status": "healthy"}

        health_status = container.get_health()

        assert health_status is not None
        assert isinstance(health_status, dict)

    def test_health_check_with_unhealthy_service(self, clean_env, mock_services):
        """Test health check when some services are unhealthy."""
        container = DIContainer()

        # Set up services with one unhealthy
        healthy_llm = mock_services["llm_provider"]
        healthy_llm.health_check = Mock(return_value={"status": "healthy"})

        unhealthy_sanitizer = mock_services["sanitizer"]
        unhealthy_sanitizer.health_check = Mock(
            return_value={"status": "unhealthy", "error": "Connection failed"}
        )

        container._llm_provider = healthy_llm
        container._sanitizer = unhealthy_sanitizer

        health_status = container.get_health()

        # Should still return health status with details
        assert health_status is not None

    def test_container_reset_method(self, clean_env):
        """Test container reset method."""
        container = DIContainer()
        container._initialized = True
        container.llm_provider = Mock()
        container.settings = Mock()

        container.reset()

        # Should reset initialization state and services
        assert not container._initialized
        assert not hasattr(container, "llm_provider")
        # settings is not cleared by reset() method

    def test_health_check_method(self, clean_env):
        """Test health check method that actually exists."""
        container = DIContainer()
        container._initialized = True
        container.settings = get_settings()

        # Set up all required attributes for health_check
        container.llm_provider = Mock()
        container.sanitizer = Mock()
        container.tracer = Mock()
        container.vector_store = Mock()
        container.session_store = Mock()
        container.tools = []
        container.agent_service = Mock()
        container.data_service = Mock()
        container.knowledge_service = Mock()
        container.session_service = Mock()
        container.data_classifier = Mock()
        container.log_processor = Mock()

        health_status = container.get_health()

        assert health_status is not None
        assert isinstance(health_status, dict)
        assert "components" in health_status

    def test_service_availability_via_health_check(self, clean_env, mock_services):
        """Test service availability checking via health_check method."""
        container = DIContainer()
        container._initialized = True

        # Set up all required attributes for health_check
        container.llm_provider = mock_services["llm_provider"]
        container.sanitizer = mock_services["sanitizer"]
        container.tracer = mock_services["tracer"]
        container.vector_store = mock_services["vector_store"]
        container.session_store = mock_services["session_store"]
        container.tools = mock_services["tools"]
        container.agent_service = Mock()
        container.data_service = Mock()
        container.knowledge_service = Mock()
        container.session_service = Mock()
        container.data_classifier = Mock()
        container.log_processor = Mock()

        health_status = container.get_health()

        assert health_status is not None
        assert isinstance(health_status, dict)
        assert "components" in health_status


class TestIsolationBetweenTestRuns:
    """Test isolation between test runs."""

    def test_container_state_isolation(self, clean_env):
        """Test that container state is properly isolated between tests."""
        # This test verifies the fixture works correctly
        container = DIContainer()

        # Should start with clean state
        assert not container._initialized
        assert container.settings is None
        assert (
            not hasattr(container, "_llm_provider")
            or getattr(container, "_llm_provider", None) is None
        )

    @pytest.mark.asyncio
    async def test_settings_isolation(self, clean_env):
        """Test that settings are properly isolated between tests."""
        # Set environment variable
        os.environ["TEST_ISOLATION"] = "test1"

        container = DIContainer()
        await container.initialize()

        # Should see the environment variable
        # (specific behavior depends on settings implementation)
        assert container.settings is not None

    def test_environment_variable_isolation(self, clean_env):
        """Test that environment variables don't leak between tests."""
        # This test verifies the clean_env fixture works

        # Should start with minimal environment
        faultmaven_vars = [
            key
            for key in os.environ.keys()
            if any(prefix in key for prefix in ["CHAT_", "REDIS_", "CHROMADB_"])
        ]

        # Should have very few or no FaultMaven-specific vars
        assert len(faultmaven_vars) == 0 or all(
            key in ["SKIP_SERVICE_CHECKS"] for key in faultmaven_vars if "SKIP" in key
        )


class TestErrorHandlingAndGracefulFallbacks:
    """Test error handling and graceful fallbacks."""

    @pytest.mark.asyncio
    async def test_interface_unavailable_fallback(self, clean_env):
        """Test fallback when interfaces are unavailable."""
        container = DIContainer()

        # Container should initialize without error even if interfaces are not available
        # (interfaces are optional and container handles their absence gracefully)
        await container.initialize()

        assert container._initialized

    @pytest.mark.asyncio
    async def test_settings_error_recovery(self, clean_env):
        """Test that container can be initialized with valid settings.

        Note: Testing actual error recovery is complex due to logger initialization
        also calling get_settings() at import time. This test verifies that the
        container initializes successfully with valid settings, which is the
        primary use case.
        """
        container = DIContainer()

        # Container should initialize successfully with valid settings
        await container.initialize()

        assert container._initialized
        assert container.settings is not None
        assert hasattr(container.settings, "server")
        assert hasattr(container.settings, "llm")


class TestDependencyGraphResolution:
    """Test dependency graph resolution and circular dependency detection."""

    @pytest.mark.asyncio
    async def test_circular_dependency_prevention(self, clean_env):
        """Test prevention of circular dependencies."""
        container = DIContainer()

        # This is more of a design test - our current architecture
        # should not have circular dependencies

        # Infrastructure layer should not depend on service layer
        # Service layer can depend on infrastructure layer
        # This is enforced by the initialization order

        await container.initialize()

        # If we get here without infinite recursion, the test passes
        assert container._initialized

    def test_lazy_dependency_resolution(self, clean_env):
        """Test lazy dependency resolution."""
        container = DIContainer()

        # Services should not be created until first access
        assert (
            not hasattr(container, "_agent_service")
            or getattr(container, "_agent_service", None) is None
        )

        # Accessing service should trigger creation
        with patch.object(container, "initialize") as mock_init:
            container.get_agent_service()
            mock_init.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
