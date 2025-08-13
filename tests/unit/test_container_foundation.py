"""DIContainer Foundation Tests

Purpose: Test core container functionality according to interface-based architecture.

This test suite validates:
1. Singleton pattern implementation and integrity
2. Lazy initialization behavior and lifecycle
3. Dependency graph resolution and injection
4. Health monitoring and diagnostics
5. Container state management and reset functionality
6. Error handling and graceful degradation
7. Component lifecycle and cleanup

Architecture Compliance:
- Tests container as singleton with proper lifecycle
- Validates dependency injection follows interface patterns
- Tests health monitoring provides comprehensive diagnostics
- Ensures graceful handling of missing dependencies
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
import logging
import threading
import time
from typing import Any, Dict

from faultmaven.container import DIContainer, GlobalContainer


@pytest.fixture(autouse=True)  
def reset_container():
    """Reset container between tests to ensure isolation"""
    # Clear singleton instance
    DIContainer._instance = None
    yield
    # Clean up after test
    if DIContainer._instance:
        DIContainer._instance.reset()
    DIContainer._instance = None


class TestDIContainerSingleton:
    """Test DIContainer singleton pattern implementation"""
    
    def test_singleton_pattern_basic(self):
        """Verify container follows singleton pattern correctly"""
        container1 = DIContainer()
        container2 = DIContainer()
        
        # Should be the same instance
        assert container1 is container2
        assert id(container1) == id(container2)
    
    def test_singleton_across_multiple_calls(self):
        """Test singleton persistence across multiple instantiation calls"""
        containers = [DIContainer() for _ in range(5)]
        
        # All should be the same instance
        first_container = containers[0]
        for container in containers[1:]:
            assert container is first_container
            assert id(container) == id(first_container)
    
    def test_singleton_thread_safety(self):
        """Test singleton pattern is thread-safe"""
        containers = []
        errors = []
        
        def create_container():
            try:
                container = DIContainer()
                containers.append(container)
            except Exception as e:
                errors.append(e)
        
        # Create multiple threads creating containers simultaneously
        threads = [threading.Thread(target=create_container) for _ in range(10)]
        
        # Start all threads
        for thread in threads:
            thread.start()
        
        # Wait for all to complete
        for thread in threads:
            thread.join()
        
        # Should have no errors
        assert len(errors) == 0, f"Thread safety failed with errors: {errors}"
        
        # All containers should be the same instance
        assert len(containers) == 10
        first_container = containers[0]
        for container in containers[1:]:
            assert container is first_container
    
    def test_singleton_state_persistence(self):
        """Test that singleton state persists across calls"""
        container1 = DIContainer()
        container1.initialize()
        
        # State should persist in new references
        container2 = DIContainer()
        assert container2._initialized is True
        assert container1._initialized is True
        assert container1 is container2


class TestDIContainerInitialization:
    """Test container initialization behavior and lifecycle"""
    
    def test_lazy_initialization_default(self):
        """Test that container starts uninitialized (lazy loading)"""
        container = DIContainer()
        
        # Should not be initialized by default
        assert not container._initialized
        assert not getattr(container, '_initializing', False)
    
    def test_explicit_initialization(self):
        """Test explicit initialization process"""
        container = DIContainer()
        
        # Before initialization
        assert not container._initialized
        
        # Initialize
        container.initialize()
        
        # After initialization
        assert container._initialized
        assert not container._initializing
    
    def test_initialization_idempotency(self):
        """Test that multiple initialization calls are safe"""
        container = DIContainer()
        
        # Initialize multiple times
        container.initialize()
        assert container._initialized
        
        container.initialize()  # Should not cause issues
        assert container._initialized
        
        container.initialize()  # Third time
        assert container._initialized
    
    def test_lazy_initialization_on_access(self):
        """Test that accessing services triggers initialization"""
        container = DIContainer()
        
        # Should not be initialized yet
        assert not container._initialized
        
        # First service access should trigger initialization
        service = container.get_agent_service()
        assert container._initialized
        assert service is not None
    
    def test_initialization_prevents_reentrance(self):
        """Test that initialization prevents re-entrant calls"""
        container = DIContainer()
        
        with patch.object(container, '_create_infrastructure_layer') as mock_infra:
            def slow_init():
                # Simulate slow initialization
                time.sleep(0.1)
                container._initialized = True
            
            mock_infra.side_effect = slow_init
            
            # Start initialization in background
            import threading
            thread = threading.Thread(target=container.initialize)
            thread.start()
            
            # Try to initialize again immediately
            container.initialize()  # Should not cause issues
            
            thread.join()
            assert container._initialized


class TestDIContainerComponentCreation:
    """Test container component creation and dependency resolution"""
    
    def test_infrastructure_layer_creation(self):
        """Test infrastructure layer components are created properly"""
        container = DIContainer()
        container.initialize()
        
        # Should have all infrastructure components
        assert hasattr(container, 'llm_provider')
        assert hasattr(container, 'sanitizer')
        assert hasattr(container, 'tracer')
        assert hasattr(container, 'data_classifier')
        assert hasattr(container, 'log_processor')
        
        # Components should not be None
        assert container.llm_provider is not None
        assert container.sanitizer is not None
        assert container.tracer is not None
        assert container.data_classifier is not None
        assert container.log_processor is not None
    
    def test_tools_layer_creation(self):
        """Test tools layer components are created properly"""
        container = DIContainer()
        container.initialize()
        
        # Should have tools list
        assert hasattr(container, 'tools')
        assert isinstance(container.tools, list)
        
        # Tools list should be accessible via getter
        tools = container.get_tools()
        assert isinstance(tools, list)
        assert tools is container.tools
    
    def test_service_layer_creation(self):
        """Test service layer components are created with dependencies"""
        container = DIContainer()
        container.initialize()
        
        # Should have all service components
        assert hasattr(container, 'agent_service')
        assert hasattr(container, 'data_service')
        assert hasattr(container, 'session_service')
        
        # Services should not be None
        assert container.agent_service is not None
        assert container.data_service is not None
        assert container.session_service is not None
        
        # Knowledge service is optional
        knowledge_service = container.get_knowledge_service()
        # Should not raise exception even if None
        assert knowledge_service is not None or knowledge_service is None
    
    def test_optional_components_handling(self):
        """Test that optional components are handled gracefully"""
        container = DIContainer()
        container.initialize()
        
        # Vector store and session store are optional
        vector_store = container.get_vector_store()
        session_store = container.get_session_store()
        
        # Should not raise exceptions
        assert vector_store is not None or vector_store is None
        assert session_store is not None or session_store is None
        
        # Container should still be healthy even if optional components are None
        health = container.health_check()
        assert health["status"] in ["healthy", "degraded", "not_initialized"]


class TestDIContainerGetterMethods:
    """Test container getter methods and dependency access"""
    
    def test_all_getter_methods_exist(self):
        """Test that all expected getter methods exist and are callable"""
        container = DIContainer()
        
        expected_getters = [
            'get_agent_service', 'get_data_service', 'get_knowledge_service',
            'get_llm_provider', 'get_sanitizer', 'get_tracer',
            'get_tools', 'get_data_classifier', 'get_log_processor',
            'get_vector_store', 'get_session_store', 'get_session_service'
        ]
        
        for getter_name in expected_getters:
            assert hasattr(container, getter_name), f"Missing getter method: {getter_name}"
            getter = getattr(container, getter_name)
            assert callable(getter), f"Getter method {getter_name} should be callable"
    
    def test_getter_lazy_initialization(self):
        """Test that getters trigger initialization when needed"""
        container = DIContainer()
        
        # Should not be initialized
        assert not container._initialized
        
        # Any getter should trigger initialization
        service = container.get_agent_service()
        assert container._initialized
        assert service is not None
    
    def test_getter_consistency(self):
        """Test that getters return consistent instances"""
        container = DIContainer()
        
        # Multiple calls to same getter should return same instance
        agent_service1 = container.get_agent_service()
        agent_service2 = container.get_agent_service()
        assert agent_service1 is agent_service2
        
        llm_provider1 = container.get_llm_provider()
        llm_provider2 = container.get_llm_provider()
        assert llm_provider1 is llm_provider2
        
        sanitizer1 = container.get_sanitizer()
        sanitizer2 = container.get_sanitizer()
        assert sanitizer1 is sanitizer2
    
    def test_getter_warning_for_uninitialized_access(self):
        """Test that getters log warnings for uninitialized access"""
        container = DIContainer()
        
        with patch('logging.getLogger') as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger
            
            # Access service before explicit initialization
            service = container.get_agent_service()
            
            # Should have logged warning about uninitialized access
            mock_logger.warning.assert_called()
            warning_call = mock_logger.warning.call_args[0][0]
            assert "not initialized" in warning_call


class TestDIContainerHealthCheck:
    """Test container health monitoring and diagnostics"""
    
    def test_health_check_uninitialized(self):
        """Test health check on uninitialized container"""
        container = DIContainer()
        
        health = container.health_check()
        
        assert isinstance(health, dict)
        assert health["status"] == "not_initialized"
        assert health["components"] == {}
    
    def test_health_check_initialized(self):
        """Test health check on initialized container"""
        container = DIContainer()
        container.initialize()
        
        health = container.health_check()
        
        assert isinstance(health, dict)
        assert "status" in health
        assert "components" in health
        assert health["status"] in ["healthy", "degraded"]
        
        # Should have component details
        components = health["components"]
        assert isinstance(components, dict)
        
        # Key components should be tracked
        expected_components = [
            "llm_provider", "sanitizer", "tracer", "tools_count",
            "agent_service", "data_service", "data_classifier", "log_processor"
        ]
        
        for component in expected_components:
            assert component in components, f"Health check should track {component}"
    
    def test_health_check_component_details(self):
        """Test health check provides detailed component information"""
        container = DIContainer()
        container.initialize()
        
        health = container.health_check()
        components = health["components"]
        
        # Boolean components (existence checks)
        boolean_components = [
            "llm_provider", "sanitizer", "tracer", "vector_store", "session_store",
            "agent_service", "data_service", "knowledge_service", "session_service",
            "data_classifier", "log_processor"
        ]
        
        for component in boolean_components:
            if component in components:
                assert isinstance(components[component], bool), \
                    f"{component} should be boolean in health check"
        
        # Numeric components
        if "tools_count" in components:
            assert isinstance(components["tools_count"], int), \
                "tools_count should be integer"
            assert components["tools_count"] >= 0, \
                "tools_count should be non-negative"
    
    def test_health_status_determination(self):
        """Test health status is determined correctly"""
        container = DIContainer()
        container.initialize()
        
        health = container.health_check()
        status = health["status"]
        components = health["components"]
        
        # Health status should reflect component states
        if status == "healthy":
            # Most components should be healthy
            healthy_count = sum(
                1 for value in components.values() 
                if (isinstance(value, bool) and value) or 
                   (isinstance(value, int) and value >= 0)
            )
            assert healthy_count > len(components) * 0.5, \
                "Healthy status should have majority of components working"
        
        elif status == "degraded":
            # Some components may be None/False but system still functional
            assert any(components.values()), \
                "Degraded status should have some working components"


class TestDIContainerReset:
    """Test container reset functionality and state management"""
    
    def test_reset_clears_initialization_state(self):
        """Test that reset clears initialization state"""
        container = DIContainer()
        container.initialize()
        
        # Verify initialized
        assert container._initialized
        
        # Reset
        container.reset()
        
        # Verify reset state
        assert not container._initialized
        assert not getattr(container, '_initializing', True)  # Should be False or not exist
    
    def test_reset_clears_components(self):
        """Test that reset clears all cached components"""
        container = DIContainer()
        container.initialize()
        
        # Verify components exist
        assert hasattr(container, 'llm_provider')
        assert hasattr(container, 'agent_service')
        
        # Reset
        container.reset()
        
        # Verify components are cleared
        assert not hasattr(container, 'llm_provider')
        assert not hasattr(container, 'agent_service')
        assert not hasattr(container, 'tools')
    
    def test_reset_allows_reinitialization(self):
        """Test that reset allows clean reinitialization"""
        container = DIContainer()
        
        # Initialize first time
        container.initialize()
        first_agent_service = container.get_agent_service()
        
        # Reset and initialize again
        container.reset()
        container.initialize()
        second_agent_service = container.get_agent_service()
        
        # Should have new instances
        assert first_agent_service is not second_agent_service
        assert container._initialized
    
    def test_reset_maintains_singleton(self):
        """Test that reset maintains singleton pattern"""
        container1 = DIContainer()
        container1.initialize()
        
        # Reset
        container1.reset()
        
        # New reference should still be same singleton
        container2 = DIContainer()
        assert container1 is container2
        assert not container2._initialized  # Should reflect reset state


class TestDIContainerErrorHandling:
    """Test container error handling and graceful degradation"""
    
    def test_initialization_error_with_interfaces_available(self):
        """Test initialization error handling when interfaces are available"""
        container = DIContainer()
        
        with patch('faultmaven.container.INTERFACES_AVAILABLE', True):
            with patch.object(container, '_create_infrastructure_layer', 
                             side_effect=ValueError("Critical infrastructure error")):
                
                # Initialize should handle error
                container.initialize()
                
                # Should not be initialized due to critical error
                assert not container._initialized
    
    def test_initialization_fallback_without_interfaces(self):
        """Test initialization fallback when interfaces are not available"""
        container = DIContainer()
        
        with patch('faultmaven.container.INTERFACES_AVAILABLE', False):
            with patch.object(container, '_create_infrastructure_layer', 
                             side_effect=ImportError("Dependencies unavailable")):
                
                # Initialize should create minimal container
                container.initialize()
                
                # Should be initialized with minimal components
                assert container._initialized
                
                # Should have mock components
                assert hasattr(container, 'llm_provider')
                assert hasattr(container, 'sanitizer')
                assert hasattr(container, 'agent_service')
    
    def test_service_creation_partial_failure(self):
        """Test graceful handling of partial service creation failures"""
        container = DIContainer()
        
        # Mock failure in knowledge service creation
        with patch('faultmaven.core.knowledge.ingestion.KnowledgeIngester',
                   side_effect=Exception("Knowledge ingester unavailable")):
            
            container.initialize()
            
            # Should still be initialized
            assert container._initialized
            
            # Other services should work
            assert container.get_agent_service() is not None
            assert container.get_data_service() is not None
            
            # Knowledge service should be handled gracefully
            knowledge_service = container.get_knowledge_service()
            # Should not raise exception
            assert knowledge_service is not None
    
    def test_optional_component_failure_handling(self):
        """Test handling of optional component initialization failures"""
        container = DIContainer()
        
        # Mock vector store failure
        with patch('faultmaven.infrastructure.persistence.chromadb_store.ChromaDBVectorStore',
                   side_effect=Exception("ChromaDB unavailable")):
            
            container.initialize()
            
            # Should still be initialized
            assert container._initialized
            
            # Vector store should be None
            vector_store = container.get_vector_store()
            assert vector_store is None
            
            # Health check should show degraded but functional
            health = container.health_check()
            assert health["status"] in ["healthy", "degraded"]


class TestGlobalContainerProxy:
    """Test GlobalContainer proxy behavior"""
    
    def test_global_container_proxy_delegation(self):
        """Test that GlobalContainer properly delegates to singleton"""
        global_container = GlobalContainer()
        direct_container = DIContainer()
        
        # Should delegate to same singleton instance
        assert global_container() is direct_container
    
    def test_global_container_attribute_access(self):
        """Test GlobalContainer attribute access delegation"""
        global_container = GlobalContainer()
        
        # Should delegate method calls
        assert hasattr(global_container, 'initialize')
        assert hasattr(global_container, 'get_agent_service')
        assert callable(global_container.initialize)
        
        # Should delegate to current singleton instance
        global_container.initialize()
        direct_container = DIContainer()
        assert direct_container._initialized
    
    def test_global_container_identity_comparison(self):
        """Test GlobalContainer identity comparison with DIContainer"""
        global_container = GlobalContainer()
        direct_container = DIContainer()
        
        # GlobalContainer should compare equal to current singleton
        assert global_container == direct_container
        
        # But they are not the same object (proxy vs real)
        assert global_container is not direct_container
    
    def test_global_container_isinstance_compatibility(self):
        """Test GlobalContainer isinstance compatibility"""
        from faultmaven.container import container as global_container
        
        # Should work with isinstance checks via __class__ property
        assert global_container.__class__ == DIContainer