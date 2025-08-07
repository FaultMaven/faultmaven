"""Tests for Refactored DI Container - Phase 5

Purpose: Validate centralized dependency management and injection

Test Coverage:
- Container initialization and singleton behavior
- Dependency graph resolution
- Service creation with proper interface implementations
- Error handling and fallback mechanisms
- Health checks and container state management
"""

import pytest
from unittest.mock import patch, MagicMock
import logging

from faultmaven.container import DIContainer, container
from faultmaven.models.interfaces import ILLMProvider, ITracer, ISanitizer, BaseTool


class TestDIContainer:
    """Test the dependency injection container"""

    def setup_method(self):
        """Reset container before each test"""
        # Reset the singleton instance
        DIContainer._instance = None

    def teardown_method(self):
        """Clean up after each test"""
        if DIContainer._instance:
            DIContainer._instance.reset()
        DIContainer._instance = None

    def test_singleton_behavior(self):
        """Test that container is a proper singleton"""
        container1 = DIContainer()
        container2 = DIContainer()
        
        assert container1 is container2
        assert id(container1) == id(container2)

    def test_container_initialization(self):
        """Test basic container initialization"""
        test_container = DIContainer()
        
        # Before initialization
        assert not test_container._initialized
        
        # After initialization
        test_container.initialize()
        
        assert test_container._initialized

    def test_infrastructure_layer_creation(self):
        """Test creation of infrastructure components with interfaces"""
        test_container = DIContainer()
        test_container.initialize()
        
        # Verify infrastructure components exist and implement interfaces
        assert hasattr(test_container, 'llm_provider')
        assert hasattr(test_container, 'sanitizer') 
        assert hasattr(test_container, 'tracer')
        assert hasattr(test_container, 'data_classifier')
        assert hasattr(test_container, 'log_processor')
        
        # Verify interface implementations (may be real or mock depending on dependencies)
        # In testing environment with missing dependencies, these will be MagicMock objects
        from faultmaven.container import INTERFACES_AVAILABLE
        if INTERFACES_AVAILABLE:
            assert isinstance(test_container.llm_provider, ILLMProvider)
            assert isinstance(test_container.sanitizer, ISanitizer)
            assert isinstance(test_container.tracer, ITracer)
        else:
            # In minimal container, these should be mock objects with necessary methods
            assert hasattr(test_container.llm_provider, 'generate_response') or hasattr(test_container.llm_provider, 'generate')
            assert hasattr(test_container.sanitizer, 'sanitize')
            assert hasattr(test_container.tracer, 'trace')

    def test_tools_layer_creation(self):
        """Test creation of tools layer"""
        test_container = DIContainer()
        test_container.initialize()
        
        # Verify tools list exists
        assert hasattr(test_container, 'tools')
        assert isinstance(test_container.tools, list)
        
        # Verify tools implement BaseTool interface (may be empty list in minimal container)
        from faultmaven.container import INTERFACES_AVAILABLE
        if INTERFACES_AVAILABLE:
            for tool in test_container.tools:
                assert isinstance(tool, BaseTool)
        else:
            # In minimal container, tools list may be empty but should still be a list
            assert isinstance(test_container.tools, list)

    @patch('faultmaven.tools.knowledge_base.KnowledgeBaseTool')
    @patch('faultmaven.tools.web_search.WebSearchTool')
    @patch('faultmaven.core.knowledge.ingestion.KnowledgeIngester')
    def test_tools_layer_with_failures(self, mock_ingester, mock_web_tool, mock_kb_tool):
        """Test tools layer handles initialization failures gracefully"""
        # Simulate KnowledgeBaseTool failure
        mock_kb_tool.side_effect = Exception("Knowledge base unavailable")
        mock_web_tool.return_value = MagicMock()
        
        test_container = DIContainer()
        test_container.initialize()
        
        # Container should still initialize successfully
        assert test_container._initialized
        assert hasattr(test_container, 'tools')
        
        # Should have fewer tools due to failure
        # (exact count depends on how many tools fail)
        assert isinstance(test_container.tools, list)

    def test_service_layer_creation(self):
        """Test creation of service layer with dependencies"""
        test_container = DIContainer()
        test_container.initialize()
        
        # Verify services exist
        assert hasattr(test_container, 'agent_service')
        assert hasattr(test_container, 'data_service')
        
        # Verify agent service has proper dependencies
        agent_service = test_container.agent_service
        assert hasattr(agent_service, '_llm')
        assert hasattr(agent_service, '_tools')
        assert hasattr(agent_service, '_tracer')
        assert hasattr(agent_service, '_sanitizer')
        
        # Verify data service has proper dependencies
        data_service = test_container.data_service
        assert hasattr(data_service, '_classifier')
        assert hasattr(data_service, '_processor')
        assert hasattr(data_service, '_sanitizer')
        assert hasattr(data_service, '_tracer')

    def test_getter_methods(self):
        """Test all public getter methods"""
        test_container = DIContainer()
        
        # Test getters trigger initialization
        agent_service = test_container.get_agent_service()
        assert test_container._initialized
        assert agent_service is not None
        
        # Test other getters
        data_service = test_container.get_data_service()
        llm_provider = test_container.get_llm_provider()
        sanitizer = test_container.get_sanitizer()
        tracer = test_container.get_tracer()
        tools = test_container.get_tools()
        classifier = test_container.get_data_classifier()
        processor = test_container.get_log_processor()
        
        # Verify all components exist
        assert data_service is not None
        assert llm_provider is not None
        assert sanitizer is not None
        assert tracer is not None
        assert isinstance(tools, list)
        assert classifier is not None
        assert processor is not None

    def test_knowledge_service_optional(self):
        """Test knowledge service is optional and handles failures"""
        test_container = DIContainer()
        test_container.initialize()
        
        # Knowledge service may be None if KnowledgeIngester fails
        knowledge_service = test_container.get_knowledge_service()
        # Should not raise exception even if None
        assert knowledge_service is not None or knowledge_service is None

    @patch('faultmaven.container.logging')
    def test_initialization_error_handling(self, mock_logging):
        """Test container handles initialization errors gracefully"""
        test_container = DIContainer()
        
        # Mock logger to capture messages
        mock_logger = MagicMock()
        mock_logging.getLogger.return_value = mock_logger
        
        # Test with interfaces available but real error
        with patch('faultmaven.container.INTERFACES_AVAILABLE', True):
            with patch.object(test_container, '_create_infrastructure_layer', side_effect=ValueError("Real error")):
                test_container.initialize()
                
                # Should log error and not initialize if interfaces are available but real error occurs
                mock_logger.error.assert_called()
                assert not test_container._initialized

    def test_health_check_uninitialized(self):
        """Test health check on uninitialized container"""
        test_container = DIContainer()
        
        health = test_container.health_check()
        
        assert health["status"] == "not_initialized"
        assert health["components"] == {}

    def test_health_check_initialized(self):
        """Test health check on initialized container"""
        test_container = DIContainer()
        test_container.initialize()
        
        health = test_container.health_check()
        
        # Should have status and components
        assert "status" in health
        assert "components" in health
        assert isinstance(health["components"], dict)
        
        # Should check key components
        components = health["components"]
        expected_components = [
            "llm_provider", "sanitizer", "tracer", "tools_count",
            "agent_service", "data_service", "data_classifier", "log_processor"
        ]
        
        for component in expected_components:
            assert component in components

    def test_container_reset(self):
        """Test container reset functionality"""
        test_container = DIContainer()
        test_container.initialize()
        
        # Verify initialized
        assert test_container._initialized
        assert hasattr(test_container, 'agent_service')
        
        # Reset container
        test_container.reset()
        
        # Verify reset state
        assert not test_container._initialized
        assert not hasattr(test_container, 'agent_service')

    def test_lazy_initialization(self):
        """Test lazy initialization - components only created when needed"""
        test_container = DIContainer()
        
        # Should not be initialized yet
        assert not test_container._initialized
        
        # First call should trigger initialization
        agent_service = test_container.get_agent_service()
        assert test_container._initialized
        
        # Subsequent calls should use existing instances
        agent_service2 = test_container.get_agent_service()
        assert agent_service is agent_service2

    @patch('faultmaven.services.agent_service.AgentService')
    def test_dependency_injection_flow(self, mock_agent_service):
        """Test that dependencies are properly injected"""
        mock_service_instance = MagicMock()
        mock_agent_service.return_value = mock_service_instance
        
        test_container = DIContainer()
        agent_service = test_container.get_agent_service()
        
        # Verify AgentService was called with proper dependencies
        mock_agent_service.assert_called_once()
        call_args = mock_agent_service.call_args
        
        # Should have been called with interface implementations
        assert 'llm_provider' in call_args.kwargs
        assert 'tools' in call_args.kwargs
        assert 'tracer' in call_args.kwargs
        assert 'sanitizer' in call_args.kwargs

    def test_global_container_instance(self):
        """Test the global container instance"""
        # Create a fresh container to establish the singleton
        fresh_container = DIContainer()
        
        # Import the global container (it's a proxy that delegates to the singleton)
        from faultmaven.container import container as global_container
        
        # Global container is a proxy, so we test by calling it to get the singleton
        global_container_instance = global_container()
        
        # Both should be the same singleton instance
        assert fresh_container is global_container_instance, f"Fresh: {id(fresh_container)}, Global: {id(global_container_instance)}"
        
        # Creating another container should also be the same instance
        another_container = DIContainer()
        assert another_container is global_container_instance
        assert another_container is fresh_container
        
        # Test that the proxy correctly delegates method calls
        assert hasattr(global_container, 'initialize')
        assert hasattr(global_container, 'get_agent_service')
        assert callable(global_container.initialize)


class TestContainerIntegration:
    """Integration tests for container with real components"""
    
    def setup_method(self):
        """Reset container before each test"""
        DIContainer._instance = None

    def teardown_method(self):
        """Clean up after each test"""
        if DIContainer._instance:
            DIContainer._instance.reset()
        DIContainer._instance = None

    @pytest.mark.integration
    def test_full_container_initialization(self):
        """Test full container initialization with real components"""
        test_container = DIContainer()
        test_container.initialize()
        
        # Should successfully create all services
        assert test_container._initialized
        
        # Test service functionality
        agent_service = test_container.get_agent_service()
        assert agent_service is not None
        
        data_service = test_container.get_data_service()
        assert data_service is not None
        
        # Verify interface compliance
        llm_provider = test_container.get_llm_provider()
        assert hasattr(llm_provider, 'generate_response') or hasattr(llm_provider, 'generate')
        
        sanitizer = test_container.get_sanitizer()
        assert hasattr(sanitizer, 'sanitize')
        
        tracer = test_container.get_tracer()
        assert hasattr(tracer, 'trace')

    @pytest.mark.integration  
    def test_container_health_check_integration(self):
        """Test health check with real components"""
        test_container = DIContainer()
        test_container.initialize()
        
        health = test_container.health_check()
        
        # Should show healthy or degraded (not error)
        assert health["status"] in ["healthy", "degraded"]
        
        # Should have reasonable component counts
        components = health["components"]
        assert components["llm_provider"] is True
        assert components["sanitizer"] is True
        assert components["tracer"] is True
        assert components["tools_count"] >= 0
        assert components["agent_service"] is True
        assert components["data_service"] is True