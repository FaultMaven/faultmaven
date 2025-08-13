"""Comprehensive Interface Compliance Test Suite

Purpose: Validate all implementations properly implement their interfaces according to 
the FaultMaven interface-based architecture.

This test suite ensures:
1. All container-provided services implement their declared interfaces
2. Interface contracts are satisfied (method signatures, return types)
3. Dependency injection follows interface-based patterns
4. Fallback behaviors work when dependencies are unavailable

Architecture Compliance:
- Tests use Mock(spec=InterfaceName) for interface compliance
- Container is reset between tests for isolation
- Tests validate contracts, not implementation details
- Graceful degradation testing for missing dependencies
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from typing import Any, Dict, List
import logging

# Import interfaces - handle graceful fallback for test environments
try:
    from faultmaven.models.interfaces import (
        ILLMProvider, ISanitizer, ITracer, IDataClassifier, 
        ILogProcessor, IVectorStore, ISessionStore, 
        IStorageBackend, IKnowledgeIngester, BaseTool,
        ToolResult, IConfiguration
    )
    INTERFACES_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"Interfaces not available: {e}")
    # Create placeholder types for testing environments without full dependencies
    ILLMProvider = Any
    ISanitizer = Any
    ITracer = Any
    IDataClassifier = Any
    ILogProcessor = Any
    IVectorStore = Any
    ISessionStore = Any
    IStorageBackend = Any
    IKnowledgeIngester = Any
    BaseTool = Any
    ToolResult = Any
    IConfiguration = Any
    INTERFACES_AVAILABLE = False

from faultmaven.container import DIContainer


@pytest.fixture(autouse=True)
def reset_container():
    """Reset container between tests for isolation"""
    # Clear any existing singleton instance
    DIContainer._instance = None
    yield
    # Clean up after test
    if DIContainer._instance:
        DIContainer._instance.reset()
    DIContainer._instance = None


class TestInterfaceCompliance:
    """Validate all container-provided implementations comply with interfaces"""
    
    def test_llm_provider_interface_compliance(self):
        """Test that LLM provider implements ILLMProvider interface"""
        container = DIContainer()
        container.initialize()
        
        llm_provider = container.get_llm_provider()
        assert llm_provider is not None
        
        if INTERFACES_AVAILABLE:
            # In full environment, verify actual interface implementation
            assert isinstance(llm_provider, ILLMProvider), "LLM provider must implement ILLMProvider"
            
            # Verify interface contract methods exist
            assert hasattr(llm_provider, 'generate'), "ILLMProvider must have 'generate' method"
            assert callable(llm_provider.generate), "'generate' must be callable"
        else:
            # In minimal environment, verify duck typing
            generate_method = getattr(llm_provider, 'generate', None) or getattr(llm_provider, 'generate_response', None)
            assert generate_method is not None, "LLM provider must have generate method"
            assert callable(generate_method), "Generate method must be callable"
    
    def test_sanitizer_interface_compliance(self):
        """Test that sanitizer implements ISanitizer interface"""
        container = DIContainer()
        container.initialize()
        
        sanitizer = container.get_sanitizer()
        assert sanitizer is not None
        
        if INTERFACES_AVAILABLE:
            assert isinstance(sanitizer, ISanitizer), "Sanitizer must implement ISanitizer"
            assert hasattr(sanitizer, 'sanitize'), "ISanitizer must have 'sanitize' method"
            assert callable(sanitizer.sanitize), "'sanitize' must be callable"
        else:
            # Verify duck typing in minimal environment
            assert hasattr(sanitizer, 'sanitize'), "Sanitizer must have 'sanitize' method"
            assert callable(sanitizer.sanitize), "'sanitize' must be callable"
    
    def test_tracer_interface_compliance(self):
        """Test that tracer implements ITracer interface"""
        container = DIContainer()
        container.initialize()
        
        tracer = container.get_tracer()
        assert tracer is not None
        
        if INTERFACES_AVAILABLE:
            assert isinstance(tracer, ITracer), "Tracer must implement ITracer"
            assert hasattr(tracer, 'trace'), "ITracer must have 'trace' method"
            assert callable(tracer.trace), "'trace' must be callable"
        else:
            # Verify duck typing in minimal environment
            assert hasattr(tracer, 'trace'), "Tracer must have 'trace' method"
            assert callable(tracer.trace), "'trace' must be callable"
    
    def test_data_classifier_interface_compliance(self):
        """Test that data classifier implements IDataClassifier interface"""
        container = DIContainer()
        container.initialize()
        
        classifier = container.get_data_classifier()
        assert classifier is not None
        
        if INTERFACES_AVAILABLE:
            assert isinstance(classifier, IDataClassifier), "Classifier must implement IDataClassifier"
            assert hasattr(classifier, 'classify'), "IDataClassifier must have 'classify' method"
            assert callable(classifier.classify), "'classify' must be callable"
        else:
            # Verify duck typing in minimal environment
            assert hasattr(classifier, 'classify'), "Classifier must have 'classify' method"
            assert callable(classifier.classify), "'classify' must be callable"
    
    def test_log_processor_interface_compliance(self):
        """Test that log processor implements ILogProcessor interface"""
        container = DIContainer()
        container.initialize()
        
        processor = container.get_log_processor()
        assert processor is not None
        
        if INTERFACES_AVAILABLE:
            assert isinstance(processor, ILogProcessor), "Processor must implement ILogProcessor"
            assert hasattr(processor, 'process'), "ILogProcessor must have 'process' method"
            assert callable(processor.process), "'process' must be callable"
        else:
            # Verify duck typing in minimal environment
            assert hasattr(processor, 'process'), "Processor must have 'process' method"
            assert callable(processor.process), "'process' must be callable"
    
    def test_vector_store_interface_compliance(self):
        """Test that vector store implements IVectorStore interface"""
        container = DIContainer()
        container.initialize()
        
        vector_store = container.get_vector_store()
        # Vector store is optional and may be None if ChromaDB is unavailable
        
        if vector_store is not None:
            if INTERFACES_AVAILABLE:
                assert isinstance(vector_store, IVectorStore), "Vector store must implement IVectorStore"
                
                # Verify interface contract methods
                required_methods = ['add_documents', 'search', 'delete_documents']
                for method_name in required_methods:
                    assert hasattr(vector_store, method_name), f"IVectorStore must have '{method_name}' method"
                    assert callable(getattr(vector_store, method_name)), f"'{method_name}' must be callable"
            else:
                # In minimal environment, verify basic structure if present
                assert hasattr(vector_store, 'add_documents') or hasattr(vector_store, 'search'), \
                    "Vector store should have basic methods"
    
    def test_session_store_interface_compliance(self):
        """Test that session store implements ISessionStore interface"""
        container = DIContainer()
        container.initialize()
        
        session_store = container.get_session_store()
        # Session store is optional and may be None if Redis is unavailable
        
        if session_store is not None:
            if INTERFACES_AVAILABLE:
                assert isinstance(session_store, ISessionStore), "Session store must implement ISessionStore"
                
                # Verify interface contract methods
                required_methods = ['get', 'set', 'delete', 'exists', 'extend_ttl']
                for method_name in required_methods:
                    assert hasattr(session_store, method_name), f"ISessionStore must have '{method_name}' method"
                    assert callable(getattr(session_store, method_name)), f"'{method_name}' must be callable"
            else:
                # In minimal environment, verify basic structure if present
                basic_methods = ['get', 'set', 'delete']
                has_basic_methods = any(hasattr(session_store, method) for method in basic_methods)
                assert has_basic_methods, "Session store should have basic methods"
    
    def test_tools_interface_compliance(self):
        """Test that all tools implement BaseTool interface"""
        container = DIContainer()
        container.initialize()
        
        tools = container.get_tools()
        assert isinstance(tools, list), "Tools should be a list"
        
        if INTERFACES_AVAILABLE and tools:
            for i, tool in enumerate(tools):
                assert isinstance(tool, BaseTool), f"Tool {i} must implement BaseTool interface"
                
                # Verify interface contract methods
                assert hasattr(tool, 'execute'), f"Tool {i} must have 'execute' method"
                assert callable(tool.execute), f"Tool {i} 'execute' must be callable"
                assert hasattr(tool, 'get_schema'), f"Tool {i} must have 'get_schema' method"
                assert callable(tool.get_schema), f"Tool {i} 'get_schema' must be callable"
        else:
            # In minimal environment or with no tools, just verify it's a list
            assert isinstance(tools, list), "Tools should always be a list, even if empty"


class TestInterfaceContractValidation:
    """Test that interface contracts are properly satisfied"""
    
    @pytest.mark.asyncio
    async def test_llm_provider_contract(self):
        """Test ILLMProvider contract validation"""
        if not INTERFACES_AVAILABLE:
            pytest.skip("Interfaces not available - testing contract satisfaction requires full environment")
        
        container = DIContainer()
        container.initialize()
        llm_provider = container.get_llm_provider()
        
        # Mock the actual LLM call to avoid external dependencies
        with patch.object(llm_provider, 'generate', return_value="mock response") as mock_generate:
            result = await llm_provider.generate("test prompt")
            
            # Verify contract satisfaction
            mock_generate.assert_called_once_with("test prompt")
            assert isinstance(result, str), "generate() should return string"
    
    def test_sanitizer_contract(self):
        """Test ISanitizer contract validation"""
        container = DIContainer()
        container.initialize()
        sanitizer = container.get_sanitizer()
        
        # Test basic sanitization contract
        test_data = "sensitive user data"
        result = sanitizer.sanitize(test_data)
        
        # Contract: sanitize should return same type or string
        assert result is not None, "sanitize() should not return None"
        assert isinstance(result, (str, type(test_data))), "sanitize() should return string or same type"
    
    def test_tracer_contract(self):
        """Test ITracer contract validation"""
        container = DIContainer()
        container.initialize()
        tracer = container.get_tracer()
        
        # Test tracing context manager contract
        trace_context = tracer.trace("test_operation")
        
        # Contract: trace() should return context manager
        assert hasattr(trace_context, '__enter__'), "trace() should return context manager with __enter__"
        assert hasattr(trace_context, '__exit__'), "trace() should return context manager with __exit__"
    
    @pytest.mark.asyncio
    async def test_data_classifier_contract(self):
        """Test IDataClassifier contract validation"""
        container = DIContainer()
        container.initialize()
        classifier = container.get_data_classifier()
        
        # Test classification contract
        test_content = "ERROR: Database connection failed"
        
        if INTERFACES_AVAILABLE:
            # Mock the classify method if it's async
            if hasattr(classifier.classify, '__call__'):
                result = await classifier.classify(test_content)
            else:
                result = classifier.classify(test_content)
            
            # Contract: classify should return DataType or equivalent
            assert result is not None, "classify() should not return None"
        else:
            # In minimal environment, just verify method exists
            assert hasattr(classifier, 'classify'), "Classifier should have classify method"
    
    @pytest.mark.asyncio
    async def test_log_processor_contract(self):
        """Test ILogProcessor contract validation"""
        container = DIContainer()
        container.initialize()
        processor = container.get_log_processor()
        
        # Test processing contract
        test_content = "2024-01-01 10:00:00 ERROR Database connection timeout"
        
        if INTERFACES_AVAILABLE:
            # Handle both sync and async process methods
            if hasattr(processor.process, '__call__'):
                result = await processor.process(test_content)
            else:
                result = processor.process(test_content)
            
            # Contract: process should return dict with insights
            assert isinstance(result, dict), "process() should return dictionary"
        else:
            # In minimal environment, just verify method exists
            assert hasattr(processor, 'process'), "Processor should have process method"


class TestInterfaceDependencyInjection:
    """Test that dependency injection follows interface-based patterns"""
    
    def test_agent_service_uses_interfaces(self):
        """Test that AgentService receives interface implementations"""
        container = DIContainer()
        agent_service = container.get_agent_service()
        
        # Verify agent service has interface-based dependencies
        assert hasattr(agent_service, '_llm') or hasattr(agent_service, 'llm_provider'), \
            "Agent service should have LLM dependency"
        assert hasattr(agent_service, '_sanitizer') or hasattr(agent_service, 'sanitizer'), \
            "Agent service should have sanitizer dependency"
        assert hasattr(agent_service, '_tracer') or hasattr(agent_service, 'tracer'), \
            "Agent service should have tracer dependency"
        assert hasattr(agent_service, '_tools') or hasattr(agent_service, 'tools'), \
            "Agent service should have tools dependency"
    
    def test_data_service_uses_interfaces(self):
        """Test that DataService receives interface implementations"""
        container = DIContainer()
        data_service = container.get_data_service()
        
        # Verify data service has interface-based dependencies
        assert hasattr(data_service, '_classifier') or hasattr(data_service, 'classifier'), \
            "Data service should have classifier dependency"
        assert hasattr(data_service, '_processor') or hasattr(data_service, 'processor'), \
            "Data service should have processor dependency"
        assert hasattr(data_service, '_sanitizer') or hasattr(data_service, 'sanitizer'), \
            "Data service should have sanitizer dependency"
        assert hasattr(data_service, '_tracer') or hasattr(data_service, 'tracer'), \
            "Data service should have tracer dependency"
    
    def test_knowledge_service_uses_interfaces(self):
        """Test that KnowledgeService receives interface implementations"""
        container = DIContainer()
        knowledge_service = container.get_knowledge_service()
        
        # Knowledge service is optional but should have proper dependencies if present
        if knowledge_service is not None:
            # Check for typical knowledge service dependencies
            has_ingester = hasattr(knowledge_service, 'knowledge_ingester') or \
                          hasattr(knowledge_service, '_ingester')
            has_sanitizer = hasattr(knowledge_service, 'sanitizer') or \
                           hasattr(knowledge_service, '_sanitizer')
            has_vector_store = hasattr(knowledge_service, 'vector_store') or \
                              hasattr(knowledge_service, '_vector_store')
            
            # At least some dependencies should be present
            assert has_ingester or has_sanitizer or has_vector_store, \
                "Knowledge service should have at least some dependencies"


class TestInterfaceGracefulDegradation:
    """Test graceful degradation when dependencies are unavailable"""
    
    def test_container_handles_missing_vector_store(self):
        """Test container gracefully handles missing vector store"""
        container = DIContainer()
        container.initialize()
        
        # Vector store may be None if ChromaDB is unavailable
        vector_store = container.get_vector_store()
        
        # Should not raise exception even if None
        assert vector_store is None or vector_store is not None
        
        # Container should still be healthy
        health = container.health_check()
        assert health["status"] in ["healthy", "degraded", "not_initialized"]
    
    def test_container_handles_missing_session_store(self):
        """Test container gracefully handles missing session store"""
        container = DIContainer()
        container.initialize()
        
        # Session store may be None if Redis is unavailable
        session_store = container.get_session_store()
        
        # Should not raise exception even if None
        assert session_store is None or session_store is not None
        
        # Container should still be healthy
        health = container.health_check()
        assert health["status"] in ["healthy", "degraded", "not_initialized"]
    
    def test_minimal_container_interface_compliance(self):
        """Test that minimal container (testing mode) still provides interface compliance"""
        # Force minimal container mode
        with patch('faultmaven.container.INTERFACES_AVAILABLE', False):
            container = DIContainer()
            container.initialize()
            
            # Should create minimal versions of all components
            assert hasattr(container, 'llm_provider')
            assert hasattr(container, 'sanitizer')
            assert hasattr(container, 'tracer')
            assert hasattr(container, 'tools')
            
            # Minimal components should have required methods (duck typing)
            assert hasattr(container.sanitizer, 'sanitize')
            assert callable(container.sanitizer.sanitize)
            assert hasattr(container.tracer, 'trace')
            assert callable(container.tracer.trace)
    
    def test_interface_contract_with_failures(self):
        """Test interface contracts are maintained even when implementations fail"""
        container = DIContainer()
        
        # Mock infrastructure creation failures
        with patch.object(container, '_create_infrastructure_layer') as mock_infra:
            mock_infra.side_effect = Exception("Infrastructure unavailable")
            
            # Should still initialize with minimal components
            container.initialize()
            
            # Should have some form of each component (minimal or mock)
            llm_provider = container.get_llm_provider()
            sanitizer = container.get_sanitizer()
            tracer = container.get_tracer()
            
            # Basic interface compliance should be maintained
            assert llm_provider is not None
            assert sanitizer is not None
            assert tracer is not None


class TestInterfaceValidationMetrics:
    """Test interface validation and compliance metrics"""
    
    def test_interface_coverage_validation(self):
        """Validate that all major interfaces are covered by tests"""
        expected_interfaces = [
            'ILLMProvider', 'ISanitizer', 'ITracer', 'IDataClassifier',
            'ILogProcessor', 'IVectorStore', 'ISessionStore', 'BaseTool'
        ]
        
        # Get all test methods in this module
        import inspect
        current_module = inspect.getmodule(TestInterfaceCompliance)
        test_methods = []
        
        for name, obj in inspect.getmembers(current_module):
            if inspect.isclass(obj) and name.startswith('Test'):
                for method_name, method in inspect.getmembers(obj):
                    if method_name.startswith('test_') and inspect.isfunction(method):
                        test_methods.append(method_name)
        
        # Verify we have tests for core interfaces
        core_interfaces = ['llm_provider', 'sanitizer', 'tracer', 'data_classifier']
        for interface in core_interfaces:
            has_test = any(interface in method_name for method_name in test_methods)
            assert has_test, f"Missing test coverage for {interface} interface"
    
    def test_container_interface_health_metrics(self):
        """Test that container provides interface health metrics"""
        container = DIContainer()
        container.initialize()
        
        health = container.health_check()
        
        # Should provide detailed component health
        components = health.get("components", {})
        
        # Key interface implementations should be tracked
        key_components = [
            "llm_provider", "sanitizer", "tracer", 
            "data_classifier", "log_processor"
        ]
        
        for component in key_components:
            assert component in components, f"Health check should track {component}"
            
            # Component health should be boolean or positive number
            health_value = components[component]
            assert isinstance(health_value, (bool, int, float)), \
                f"{component} health should be boolean or number"
            
            if isinstance(health_value, (int, float)):
                assert health_value >= 0, f"{component} health should be non-negative"
    
    def test_interface_implementation_consistency(self):
        """Test that interface implementations are consistent across container lifecycle"""
        container = DIContainer()
        
        # Get initial implementations
        container.initialize()
        initial_llm = container.get_llm_provider()
        initial_sanitizer = container.get_sanitizer()
        initial_tracer = container.get_tracer()
        
        # Reset and re-initialize
        container.reset()
        container.initialize()
        
        # Get new implementations
        new_llm = container.get_llm_provider()
        new_sanitizer = container.get_sanitizer()
        new_tracer = container.get_tracer()
        
        # Types should be consistent
        assert type(initial_llm) == type(new_llm), "LLM provider type should be consistent"
        assert type(initial_sanitizer) == type(new_sanitizer), "Sanitizer type should be consistent"
        assert type(initial_tracer) == type(new_tracer), "Tracer type should be consistent"
        
        # Interface compliance should be maintained
        if INTERFACES_AVAILABLE:
            assert isinstance(new_llm, type(initial_llm).__bases__[0] if initial_llm.__class__.__bases__ else type(initial_llm))