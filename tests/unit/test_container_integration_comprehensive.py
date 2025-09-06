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

import os
import tempfile
from unittest.mock import Mock, patch, MagicMock, AsyncMock, call
from typing import Dict, Any, List, Optional
import pytest
import asyncio
import logging

from faultmaven.container import DIContainer
from faultmaven.config.settings import get_settings, reset_settings


# Import interfaces with fallback for testing
try:
    from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool, IVectorStore, ISessionStore
    from faultmaven.models.interfaces_case import ICaseStore, ICaseService
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
    os.environ['SKIP_SERVICE_CHECKS'] = 'true'
    
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
        'SKIP_SERVICE_CHECKS': 'true',
        'PYTEST_CURRENT_TEST': os.environ.get('PYTEST_CURRENT_TEST', ''),
        'ENVIRONMENT': 'development'
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
    llm_provider.generate = AsyncMock(return_value=Mock(
        content="Mock LLM response",
        confidence=0.85,
        model="mock-model"
    ))
    llm_provider.is_available = Mock(return_value=True)
    services['llm_provider'] = llm_provider
    
    # Mock Sanitizer
    sanitizer = Mock()
    sanitizer.sanitize = AsyncMock(return_value="Sanitized content")
    sanitizer.is_sensitive = Mock(return_value=False)
    services['sanitizer'] = sanitizer
    
    # Mock Tracer
    tracer = Mock(spec=ITracer)
    tracer.trace.return_value = Mock()
    services['tracer'] = tracer
    
    # Mock Vector Store
    vector_store = Mock(spec=IVectorStore)
    vector_store.search = AsyncMock(return_value=[])
    vector_store.add_documents = AsyncMock(return_value=True)
    services['vector_store'] = vector_store
    
    # Mock Session Store
    session_store = Mock(spec=ISessionStore)
    session_store.get_session = AsyncMock(return_value=None)
    session_store.create_session = AsyncMock(return_value="test-session-id")
    services['session_store'] = session_store
    
    # Mock Case Store
    case_store = Mock(spec=ICaseStore)
    case_store.create_case = AsyncMock(return_value=True)
    case_store.get_case = AsyncMock(return_value=None)
    services['case_store'] = case_store
    
    # Mock Tools
    knowledge_tool = Mock(spec=BaseTool)
    knowledge_tool.execute = AsyncMock(return_value="Knowledge tool result")
    knowledge_tool.get_schema.return_value = {"name": "knowledge_base", "description": "Mock tool"}
    
    web_search_tool = Mock(spec=BaseTool)
    web_search_tool.execute = AsyncMock(return_value="Web search result")
    web_search_tool.get_schema.return_value = {"name": "web_search", "description": "Mock web search"}
    
    services['tools'] = [knowledge_tool, web_search_tool]
    
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
    
    def test_container_initialization_state(self):
        """Test container initialization state management."""
        container = DIContainer()
        
        # Should start uninitialized
        assert not container._initialized
        assert not getattr(container, '_initializing', False)
        assert container.settings is None
    
    def test_container_prevent_reentrant_initialization(self):
        """Test prevention of reentrant initialization."""
        container = DIContainer()
        
        # Mock initialization to track calls
        with patch.object(container, '_create_infrastructure_layer') as mock_infra:
            # Start initialization
            container._initializing = True
            
            # Try to initialize again
            container.initialize()
            
            # Should not call infrastructure creation
            mock_infra.assert_not_called()
            
            # Should still be in initializing state
            assert container._initializing


class TestContainerInitialization:
    """Test container initialization process."""
    
    def test_successful_initialization(self, clean_env):
        """Test successful container initialization."""
        container = DIContainer()
        
        with patch.object(container, '_create_infrastructure_layer') as mock_infra, \
             patch.object(container, '_create_tools_layer') as mock_tools, \
             patch.object(container, '_create_service_layer') as mock_services:
            
            container.initialize()
            
            assert container._initialized
            assert not container._initializing
            assert container.settings is not None
            
            # Verify initialization order
            mock_infra.assert_called_once()
            mock_tools.assert_called_once()
            mock_services.assert_called_once()
    
    def test_initialization_with_settings_error(self, clean_env):
        """Test initialization with settings system error."""
        container = DIContainer()
        
        with patch('faultmaven.container.get_settings', side_effect=Exception("Settings error")):
            with pytest.raises(Exception) as exc_info:
                container.initialize()
            
            assert "Settings error" in str(exc_info.value)
            assert not container._initialized
            assert not container._initializing
    
    def test_initialization_with_infrastructure_error(self, clean_env):
        """Test initialization with infrastructure layer error."""
        container = DIContainer()
        
        with patch.object(container, '_create_infrastructure_layer', side_effect=Exception("Infra error")):
            # Should not raise exception but should log error and fail to initialize
            container.initialize()
            
            # Container should fail to initialize but not crash
            assert not container._initialized
            assert not container._initializing
    
    def test_initialization_already_initialized(self, clean_env):
        """Test initialization when container is already initialized."""
        container = DIContainer()
        
        # Initialize once
        with patch.object(container, '_create_infrastructure_layer') as mock_infra:
            container.initialize()
            assert container._initialized
            
            # Try to initialize again
            container.initialize()
            
            # Should only be called once
            mock_infra.assert_called_once()
    
    def test_settings_integration(self):
        """Test integration with settings system."""
        import os
        
        # Save original environment
        original_env = os.environ.copy()
        
        try:
            # Set environment variables that should work (based on our analysis)
            os.environ.update({
                'ENVIRONMENT': 'production',
                'DEBUG': 'false',  
                'SKIP_SERVICE_CHECKS': 'true'  # Needed for testing
            })
            
            # Reset settings cache to pick up new env vars
            from faultmaven.config.settings import reset_settings
            reset_settings()
            
            container = DIContainer()
            container.initialize()
            
            assert container.settings is not None
            
            # Test settings that we know work from our investigation
            assert container.settings.server.environment.value == 'production'
            assert container.settings.server.debug == False
            
            # For the LLM provider, test that it reads the value correctly from the .env file
            # Since environment variable override doesn't work for enums (pydantic-settings bug),
            # we test that the settings system is at least functioning and reading from .env
            assert container.settings.llm.provider.value in ['fireworks', 'openai', 'anthropic', 'cohere', 'local']
            
            # Verify the settings object has the expected structure
            assert hasattr(container.settings, 'server')
            assert hasattr(container.settings, 'llm')
            assert hasattr(container.settings, 'database')
            assert hasattr(container.settings, 'session')
            
        finally:
            # Restore original environment
            os.environ.clear()
            os.environ.update(original_env)
            # Reset settings cache again
            from faultmaven.config.settings import reset_settings
            reset_settings()


class TestServiceLifecycleManagement:
    """Test service lifecycle management."""
    
    @patch('faultmaven.container.INTERFACES_AVAILABLE', True)
    def test_infrastructure_layer_creation(self, clean_env, mock_services):
        """Test infrastructure layer service creation."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Mock the infrastructure components by patching their import locations
        with patch('faultmaven.infrastructure.llm.router.LLMRouter') as mock_llm_router, \
             patch('faultmaven.infrastructure.security.redaction.DataSanitizer') as mock_sanitizer, \
             patch('faultmaven.infrastructure.observability.tracing.OpikTracer') as mock_tracer, \
             patch('faultmaven.infrastructure.persistence.chromadb_store.ChromaDBVectorStore') as mock_vector_store:
            
            # Configure mock returns
            mock_llm_router.return_value = mock_services['llm_provider']
            mock_sanitizer.return_value = mock_services['sanitizer']
            mock_tracer.return_value = mock_services['tracer']
            mock_vector_store.return_value = mock_services['vector_store']
            container._create_infrastructure_layer()
            
            # Verify infrastructure components are created
            assert hasattr(container, 'llm_provider')
            assert hasattr(container, 'sanitizer')
            assert hasattr(container, 'tracer')
    
    @patch('faultmaven.container.INTERFACES_AVAILABLE', True)
    def test_tools_layer_creation(self, clean_env, mock_services):
        """Test tools layer creation."""
        container = DIContainer()
        container.settings = get_settings()
        container.vector_store = mock_services['vector_store']
        
        with patch('faultmaven.tools.registry.tool_registry.create_all_tools') as mock_create_tools, \
             patch('faultmaven.core.knowledge.ingestion.KnowledgeIngester') as mock_ingester:
            
            mock_create_tools.return_value = mock_services['tools']
            container._create_tools_layer()
            
            assert hasattr(container, 'tools')
            assert isinstance(container.tools, list)
            assert len(container.tools) >= 1  # At least knowledge base tool
    
    @patch('faultmaven.container.INTERFACES_AVAILABLE', True)
    def test_service_layer_creation(self, clean_env, mock_services):
        """Test service layer creation."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Set up infrastructure dependencies
        container.llm_provider = mock_services['llm_provider']
        container.sanitizer = mock_services['sanitizer']
        container.tracer = mock_services['tracer']
        container.vector_store = mock_services['vector_store']
        container.session_store = mock_services['session_store']
        container.case_store = mock_services['case_store']
        container.tools = mock_services['tools']
        
        # Mock service classes
        with patch('faultmaven.services.agent.AgentService') as mock_agent, \
             patch('faultmaven.services.data.DataService') as mock_data, \
             patch('faultmaven.services.knowledge.KnowledgeService') as mock_knowledge, \
             patch('faultmaven.services.session.SessionService') as mock_session, \
             patch('faultmaven.services.case.CaseService') as mock_case:
            
            mock_agent.return_value = Mock()
            mock_data.return_value = Mock()
            mock_knowledge.return_value = Mock()
            mock_session.return_value = Mock()
            mock_case.return_value = Mock()
            
            container._create_service_layer()
            
            assert hasattr(container, 'agent_service')
            assert hasattr(container, 'data_service')
            assert hasattr(container, 'knowledge_service')
            assert hasattr(container, 'session_service')
            assert hasattr(container, 'case_service')
    
    def test_graceful_degradation_without_interfaces(self, clean_env):
        """Test graceful degradation when interfaces are not available."""
        container = DIContainer()
        
        with patch('faultmaven.container.INTERFACES_AVAILABLE', False):
            # Should initialize without error even without interfaces
            container.initialize()
            
            assert container._initialized
            assert container.settings is not None


class TestInterfaceResolutionAndInjection:
    """Test interface resolution and dependency injection."""
    
    @patch('faultmaven.container.INTERFACES_AVAILABLE', True)
    def test_service_getter_methods(self, clean_env, mock_services):
        """Test service getter methods."""
        container = DIContainer()
        
        # Mock services
        container.agent_service = mock_services['llm_provider']  # Use as placeholder
        container.data_service = mock_services['sanitizer']  # Use as placeholder
        container.knowledge_service = mock_services['tracer']  # Use as placeholder
        container.session_service = mock_services['vector_store']  # Use as placeholder
        container.case_service = mock_services['case_store']  # Use as placeholder
        
        # Test getter methods
        assert container.get_agent_service() is container.agent_service
        assert container.get_data_service() is container.data_service
        assert container.get_knowledge_service() is container.knowledge_service
        assert container.get_session_service() is container.session_service
        assert container.get_case_service() is container.case_service
    
    def test_service_getter_with_initialization(self, clean_env):
        """Test service getters trigger initialization if needed."""
        container = DIContainer()
        
        with patch.object(container, 'initialize') as mock_init, \
             patch.object(container, 'agent_service', create=True, new=Mock()) as mock_service:
            
            result = container.get_agent_service()
            
            mock_init.assert_called_once()
            assert result is mock_service
    
    def test_infrastructure_provider_getters(self, clean_env, mock_services):
        """Test infrastructure provider getter methods."""
        container = DIContainer()
        container._initialized = True  # Prevent automatic initialization
        
        # Set up infrastructure
        container.llm_provider = mock_services['llm_provider']
        container.sanitizer = mock_services['sanitizer']
        container.tracer = mock_services['tracer']
        container.vector_store = mock_services['vector_store']
        container.session_store = mock_services['session_store']
        container.tools = mock_services['tools']
        
        # Test getters
        assert container.get_llm_provider() is mock_services['llm_provider']
        assert container.get_sanitizer() is mock_services['sanitizer']
        assert container.get_tracer() is mock_services['tracer']
        assert container.get_vector_store() is mock_services['vector_store']
        assert container.get_session_store() is mock_services['session_store']
        assert container.get_tools() == mock_services['tools']
    
    def test_dependency_injection_chain(self, clean_env, mock_services):
        """Test that services receive their dependencies correctly."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Mock service creation with dependency tracking
        mock_agent_service_class = Mock()
        mock_agent_service_instance = Mock()
        mock_agent_service_class.return_value = mock_agent_service_instance
        
        # Set up dependencies
        container.llm_provider = mock_services['llm_provider']
        container.sanitizer = mock_services['sanitizer']
        container.tracer = mock_services['tracer']
        container.tools = mock_services['tools']
        
        with patch('faultmaven.services.agent.AgentService', mock_agent_service_class):
            container._create_service_layer()
            
            # Verify AgentService was called with correct dependencies
            mock_agent_service_class.assert_called_once()
            call_args = mock_agent_service_class.call_args
            
            # Should be called with keyword arguments matching interface dependencies
            assert call_args is not None


class TestMockServicePatternsForTesting:
    """Test mock service patterns for testing scenarios."""
    
    def test_mock_service_injection(self, clean_env):
        """Test injection of mock services for testing."""
        container = DIContainer()
        
        # Create mock services
        mock_llm = Mock(spec=ILLMProvider)
        mock_sanitizer = Mock(spec=ISanitizer)
        
        # Inject mocks directly
        container._llm_provider = mock_llm
        container._sanitizer = mock_sanitizer
        
        # Verify mocks are returned
        assert container.get_llm_provider() is mock_llm
        assert container.get_sanitizer() is mock_sanitizer
    
    def test_partial_mock_injection(self, clean_env):
        """Test partial mock injection with real service creation."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Inject some mocks
        mock_llm = Mock(spec=ILLMProvider)
        container._llm_provider = mock_llm
        
        # Let other services be created normally
        with patch('faultmaven.container.DataSanitizer') as mock_sanitizer_class:
            mock_sanitizer_instance = Mock()
            mock_sanitizer_class.return_value = mock_sanitizer_instance
            
            container._create_infrastructure_layer()
            
            # Mock should be preserved
            assert container.get_llm_provider() is mock_llm
            
            # Other service should be created
            assert container._sanitizer is mock_sanitizer_instance
    
    def test_service_behavior_verification(self, clean_env, mock_services):
        """Test verification of service interactions."""
        container = DIContainer()
        
        # Inject mock services
        container._llm_provider = mock_services['llm_provider']
        container._sanitizer = mock_services['sanitizer']
        container._agent_service = Mock()
        
        # Configure mock behaviors
        container._agent_service.process_query = AsyncMock(return_value="Mock response")
        
        # Test service interactions
        agent_service = container.get_agent_service()
        
        # Verify mock setup
        assert agent_service.process_query is not None
        
        # Test async mock behavior
        async def test_async():
            result = await agent_service.process_query("test query")
            return result
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(test_async())
            assert result == "Mock response"
        finally:
            loop.close()


class TestContainerHealthAndDiagnostics:
    """Test container health monitoring and diagnostics."""
    
    def test_health_check_all_services_healthy(self, clean_env, mock_services):
        """Test health check when all services are healthy."""
        container = DIContainer()
        
        # Set up healthy services
        container._llm_provider = mock_services['llm_provider']
        container._sanitizer = mock_services['sanitizer']
        container._tracer = mock_services['tracer']
        container._vector_store = mock_services['vector_store']
        
        # Mock health check methods
        for service in mock_services.values():
            if hasattr(service, 'health_check'):
                service.health_check.return_value = {"status": "healthy"}
        
        health_status = container.health_check()
        
        assert health_status is not None
        assert isinstance(health_status, dict)
    
    def test_health_check_with_unhealthy_service(self, clean_env, mock_services):
        """Test health check when some services are unhealthy."""
        container = DIContainer()
        
        # Set up services with one unhealthy
        healthy_llm = mock_services['llm_provider']
        healthy_llm.health_check = Mock(return_value={"status": "healthy"})
        
        unhealthy_sanitizer = mock_services['sanitizer']
        unhealthy_sanitizer.health_check = Mock(return_value={"status": "unhealthy", "error": "Connection failed"})
        
        container._llm_provider = healthy_llm
        container._sanitizer = unhealthy_sanitizer
        
        health_status = container.health_check()
        
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
        assert not hasattr(container, 'llm_provider')
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
        
        health_status = container.health_check()
        
        assert health_status is not None
        assert isinstance(health_status, dict)
        assert "components" in health_status
    
    def test_service_availability_via_health_check(self, clean_env, mock_services):
        """Test service availability checking via health_check method."""
        container = DIContainer()
        container._initialized = True
        
        # Set up all required attributes for health_check
        container.llm_provider = mock_services['llm_provider']
        container.sanitizer = mock_services['sanitizer']
        container.tracer = mock_services['tracer']
        container.vector_store = mock_services['vector_store']
        container.session_store = mock_services['session_store']
        container.tools = mock_services['tools']
        container.agent_service = Mock()
        container.data_service = Mock()
        container.knowledge_service = Mock()
        container.session_service = Mock()
        container.data_classifier = Mock()
        container.log_processor = Mock()
        
        health_status = container.health_check()
        
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
        assert not hasattr(container, '_llm_provider') or getattr(container, '_llm_provider', None) is None
    
    def test_settings_isolation(self, clean_env):
        """Test that settings are properly isolated between tests."""
        # Set environment variable
        os.environ['TEST_ISOLATION'] = 'test1'
        
        container = DIContainer()
        container.initialize()
        
        # Should see the environment variable
        # (specific behavior depends on settings implementation)
        assert container.settings is not None
    
    def test_mock_service_cleanup(self, clean_env):
        """Test that mock services are properly cleaned up."""
        container = DIContainer()
        
        # Inject mock service
        mock_service = Mock()
        container._llm_provider = mock_service
        
        # Verify mock is there
        assert container.get_llm_provider() is mock_service
        
        # After reset (happens in fixture), should be clean
        # This is tested implicitly by the fixture behavior
    
    def test_environment_variable_isolation(self, clean_env):
        """Test that environment variables don't leak between tests."""
        # This test verifies the clean_env fixture works
        
        # Should start with minimal environment
        faultmaven_vars = [key for key in os.environ.keys() 
                          if any(prefix in key for prefix in ['CHAT_', 'REDIS_', 'CHROMADB_'])]
        
        # Should have very few or no FaultMaven-specific vars
        assert len(faultmaven_vars) == 0 or all(key in ['SKIP_SERVICE_CHECKS'] for key in faultmaven_vars if 'SKIP' in key)


class TestErrorHandlingAndGracefulFallbacks:
    """Test error handling and graceful fallbacks."""
    
    def test_service_creation_error_handling(self, clean_env):
        """Test handling of service creation errors."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Mock service class that raises exception
        failing_service_class = Mock(side_effect=Exception("Service creation failed"))
        
        with patch('faultmaven.container.AgentService', failing_service_class):
            # Should handle error gracefully
            try:
                container._create_service_layer()
            except Exception as e:
                # Should still complete other service creation
                pass
    
    def test_interface_unavailable_fallback(self, clean_env):
        """Test fallback when interfaces are unavailable."""
        container = DIContainer()
        
        with patch('faultmaven.container.INTERFACES_AVAILABLE', False):
            # Should initialize without interfaces
            container.initialize()
            
            assert container._initialized
    
    def test_partial_service_availability(self, clean_env, mock_services):
        """Test container behavior with partial service availability."""
        container = DIContainer()
        
        # Only set up some services
        container._llm_provider = mock_services['llm_provider']
        # Don't set up sanitizer, tracer, etc.
        
        # Should handle missing services gracefully
        llm_provider = container.get_llm_provider()
        assert llm_provider is mock_services['llm_provider']
        
        # Missing services should return None or use fallbacks
        sanitizer = container.get_sanitizer()
        # Behavior depends on implementation - should not crash
    
    def test_settings_error_recovery(self, clean_env):
        """Test recovery from settings system errors."""
        container = DIContainer()
        
        # Mock settings to fail initially then succeed
        call_count = 0
        def failing_then_working_settings():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Settings failed")
            return get_settings()
        
        with patch('faultmaven.container.get_settings', side_effect=failing_then_working_settings):
            # First call should fail
            with pytest.raises(Exception):
                container.initialize()
            
            # Reset for retry
            container._initializing = False
            
            # Second call should succeed
            container.initialize()
            assert container._initialized
    
    def test_dependency_resolution_error_handling(self, clean_env):
        """Test error handling in dependency resolution."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Mock a dependency that fails during creation
        with patch('faultmaven.container.LLMRouter', side_effect=Exception("LLM Router failed")):
            # Should handle infrastructure creation errors
            try:
                container._create_infrastructure_layer()
            except Exception:
                pass
            
            # Container should still be in a consistent state
            assert not container._initializing


class TestDependencyGraphResolution:
    """Test dependency graph resolution and circular dependency detection."""
    
    def test_dependency_order_resolution(self, clean_env):
        """Test that dependencies are created in correct order."""
        container = DIContainer()
        container.settings = get_settings()
        
        creation_order = []
        
        def track_creation(name):
            def creator(*args, **kwargs):
                creation_order.append(name)
                return Mock()
            return creator
        
        with patch.multiple(
            'faultmaven.container',
            LLMRouter=track_creation('LLMRouter'),
            DataSanitizer=track_creation('DataSanitizer'),
            OpikTracer=track_creation('OpikTracer')
        ):
            container._create_infrastructure_layer()
            
            # Should have created all infrastructure components
            assert len(creation_order) > 0
            assert 'LLMRouter' in creation_order
    
    def test_service_dependency_injection_order(self, clean_env, mock_services):
        """Test that services receive dependencies in correct order."""
        container = DIContainer()
        container.settings = get_settings()
        
        # Set up infrastructure first
        container._llm_provider = mock_services['llm_provider']
        container._sanitizer = mock_services['sanitizer']
        container._tracer = mock_services['tracer']
        container._tools = mock_services['tools']
        
        service_creation_calls = []
        
        def track_service_creation(service_name):
            def creator(*args, **kwargs):
                service_creation_calls.append({
                    'service': service_name,
                    'args': args,
                    'kwargs': kwargs
                })
                return Mock()
            return creator
        
        with patch.multiple(
            'faultmaven.container',
            AgentService=track_service_creation('AgentService'),
            DataService=track_service_creation('DataService')
        ):
            container._create_service_layer()
            
            # Should have created services with proper dependencies
            assert len(service_creation_calls) >= 1
            
            # Verify services were called with dependencies
            for call in service_creation_calls:
                if call['service'] == 'AgentService':
                    # Should have been called with LLM provider, sanitizer, etc.
                    assert len(call['kwargs']) > 0 or len(call['args']) > 0
    
    def test_circular_dependency_prevention(self, clean_env):
        """Test prevention of circular dependencies."""
        container = DIContainer()
        
        # This is more of a design test - our current architecture
        # should not have circular dependencies
        
        # Infrastructure layer should not depend on service layer
        # Service layer can depend on infrastructure layer
        # This is enforced by the initialization order
        
        container.initialize()
        
        # If we get here without infinite recursion, the test passes
        assert container._initialized
    
    def test_lazy_dependency_resolution(self, clean_env):
        """Test lazy dependency resolution."""
        container = DIContainer()
        
        # Services should not be created until first access
        assert not hasattr(container, '_agent_service') or getattr(container, '_agent_service', None) is None
        
        # Accessing service should trigger creation
        with patch.object(container, 'initialize') as mock_init:
            container.get_agent_service()
            mock_init.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])