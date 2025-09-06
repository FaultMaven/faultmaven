# Modern Testing Patterns Guide

**Document Type**: Testing Patterns Guide  
**Last Updated**: August 2025  
**Context**: Post-Architecture Overhaul - Clean Architecture Testing Patterns

## Overview

This guide documents the modern testing patterns developed for FaultMaven's Clean Architecture implementation after the major test architecture overhaul. These patterns support **1425+ tests** across architectural layers with **dependency injection**, **interface-based mocking**, and **container lifecycle management**.

## Core Testing Patterns

### 1. Container-Based Testing Pattern

**Purpose**: Ensure proper dependency injection and service lifecycle management

```python
from faultmaven.container import container

def test_service_with_container_pattern():
    """Standard pattern for container-based testing."""
    # ALWAYS reset container for clean state
    container.reset()
    
    # Get service through container (dependency injection)
    agent_service = container.get_agent_service()
    
    # Validate service is properly initialized
    assert agent_service is not None
    
    # Test service functionality
    # ... test code here
    
    # Container automatically manages cleanup
```

**Key Principles**:
- Always call `container.reset()` before test logic
- Use container methods for service resolution (`container.get_*_service()`)
- Let container manage service lifecycle and dependencies
- No manual service instantiation outside of container

### 2. Interface-Based Mocking Pattern

**Purpose**: Mock dependencies through interfaces rather than concrete implementations

```python
from unittest.mock import Mock, AsyncMock
from faultmaven.models.interfaces import ILLMProvider, ISanitizer
from faultmaven.container import container

@pytest.mark.asyncio
async def test_service_with_interface_mocks():
    """Pattern for mocking dependencies through interfaces."""
    # Reset container for clean state
    container.reset()
    
    # Create interface-compliant mocks
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(return_value="Mock LLM response")
    
    mock_sanitizer = Mock(spec=ISanitizer)
    mock_sanitizer.sanitize = Mock(return_value="sanitized input")
    
    # Inject mocks through container
    container._llm_provider = mock_llm
    container._sanitizer = mock_sanitizer
    
    # Get service with injected mocks
    agent_service = container.get_agent_service()
    
    # Test service with mocked dependencies
    result = await agent_service.process_query("test query", "session-1")
    
    # Validate interactions
    mock_sanitizer.sanitize.assert_called_once_with("test query")
    mock_llm.generate_response.assert_called_once()
    
    # Validate results
    assert result.session_id == "session-1"
    assert "Mock LLM response" in result.response
```

**Key Principles**:
- Use `Mock(spec=Interface)` to ensure interface compliance
- Mock methods should match interface signatures exactly
- Use `AsyncMock` for async interface methods
- Inject mocks through container, not direct instantiation

### 3. Environment Isolation Pattern

**Purpose**: Provide clean, isolated environment configuration for each test

```python
import os
from unittest.mock import patch
from contextlib import contextmanager

@pytest.fixture
def clean_env():
    """Fixture providing clean environment isolation."""
    original_env = os.environ.copy()
    
    # Clear all FaultMaven-related environment variables
    for key in list(os.environ.keys()):
        if any(prefix in key for prefix in [
            'CHAT_', 'REDIS_', 'CHROMADB_', 'LLM_', 
            'FIREWORKS_', 'OPENAI_', 'ANTHROPIC_'
        ]):
            del os.environ[key]
    
    yield
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)

def test_settings_with_environment_isolation(clean_env):
    """Test with isolated environment configuration."""
    # Set test-specific environment
    test_config = {
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'test-key-123',
        'REDIS_HOST': '192.168.0.111',
        'REDIS_PORT': '30379'
    }
    
    with patch.dict(os.environ, test_config):
        from faultmaven.config.settings import get_settings
        
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.FIREWORKS
        assert settings.llm.fireworks_api_key == 'test-key-123'
        assert settings.database.redis_host == '192.168.0.111'
    
    # Environment automatically restored by fixture
```

**Key Principles**:
- Use fixtures for environment isolation
- Clear relevant environment variables before test
- Use `patch.dict` for temporary environment changes
- Always restore original environment after test

### 4. Cross-Layer Integration Pattern

**Purpose**: Test complete workflows through all architectural layers

```python
@pytest.mark.asyncio
async def test_cross_layer_integration():
    """Pattern for testing complete architectural workflows."""
    from faultmaven.container import container
    from faultmaven.config.settings import get_settings
    
    # Step 1: Reset container for clean state
    container.reset()
    
    # Step 2: Configure environment (Settings layer)
    test_environment = {
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-test-key',
        'REDIS_HOST': 'localhost',
        'DEBUG': 'true'
    }
    
    with patch.dict(os.environ, test_environment):
        # Step 3: Validate Settings layer
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.OPENAI
        assert settings.debug is True
        
        # Step 4: Validate Container layer (dependency resolution)
        agent_service = container.get_agent_service()
        knowledge_service = container.get_knowledge_service()
        
        assert agent_service is not None
        assert knowledge_service is not None
        
        # Step 5: Test Service layer integration
        kb_results = await knowledge_service.search_knowledge_base(
            "database connection issues",
            limit=3
        )
        assert isinstance(kb_results, list)
        
        # Step 6: Test complete workflow (API → Service → Core → Infrastructure)
        troubleshooting_result = await agent_service.process_query(
            "My database keeps timing out",
            "integration-test-session"
        )
        
        # Step 7: Validate end-to-end results
        assert troubleshooting_result.session_id == "integration-test-session"
        assert len(troubleshooting_result.response) > 100
        assert any(keyword in troubleshooting_result.response.lower() 
                  for keyword in ['database', 'timeout', 'connection'])
```

**Key Principles**:
- Test complete user workflows through all layers
- Validate each layer's functionality independently
- Test layer interactions and data flow
- Use realistic test data and scenarios

### 5. Error Handling and Graceful Degradation Pattern

**Purpose**: Test error propagation and recovery across architectural layers

```python
@pytest.mark.asyncio
async def test_error_handling_pattern():
    """Pattern for testing error handling across layers."""
    from faultmaven.container import container
    from faultmaven.models.interfaces import ILLMProvider
    from faultmaven.models.exceptions import LLMProviderError
    
    container.reset()
    
    # Create mock that simulates external service failure
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(
        side_effect=LLMProviderError("External LLM service unavailable")
    )
    
    container._llm_provider = mock_llm
    
    # Test error propagation through service layer
    agent_service = container.get_agent_service()
    
    # Test 1: Error propagation
    with pytest.raises(LLMProviderError) as exc_info:
        await agent_service.process_query("test", "error-session")
    
    assert "External LLM service unavailable" in str(exc_info.value)
    
    # Test 2: Graceful degradation (if implemented)
    # Some services might handle errors gracefully
    try:
        result = await agent_service.process_query_with_fallback(
            "test", "fallback-session"
        )
        
        # Validate fallback behavior
        assert result.session_id == "fallback-session"
        assert "fallback" in result.response.lower()
        
    except NotImplementedError:
        # Graceful degradation not yet implemented
        pytest.skip("Graceful degradation not implemented")
```

**Key Principles**:
- Test both error propagation and recovery
- Use appropriate exception types
- Test fallback mechanisms when available
- Validate error messages and context

### 6. Performance and Resource Management Pattern

**Purpose**: Test performance characteristics and resource usage

```python
import time
import psutil
import os

def test_performance_pattern():
    """Pattern for testing performance and resource usage."""
    from faultmaven.container import container
    
    # Measure container operations
    start_time = time.time()
    memory_start = psutil.Process(os.getpid()).memory_info().rss
    
    # Perform operations
    iterations = 100
    for i in range(iterations):
        container.reset()
        agent_service = container.get_agent_service()
        knowledge_service = container.get_knowledge_service()
        
        # Validate services are created
        assert agent_service is not None
        assert knowledge_service is not None
    
    # Measure results
    elapsed_time = time.time() - start_time
    memory_end = psutil.Process(os.getpid()).memory_info().rss
    memory_used = memory_end - memory_start
    
    # Validate performance requirements
    avg_time_per_operation = elapsed_time / iterations
    assert avg_time_per_operation < 0.01  # Less than 10ms per operation
    assert memory_used < 10 * 1024 * 1024  # Less than 10MB total
    
    # Log performance metrics for monitoring
    print(f"Average operation time: {avg_time_per_operation:.4f}s")
    print(f"Memory used: {memory_used / 1024 / 1024:.2f}MB")

@pytest.mark.performance
def test_conditional_performance():
    """Performance tests that run only when enabled."""
    if not os.getenv('RUN_PERFORMANCE_TESTS', '').lower() == 'true':
        pytest.skip("Performance tests disabled (set RUN_PERFORMANCE_TESTS=true)")
    
    # Performance test code here
    test_performance_pattern()
```

**Key Principles**:
- Measure actual performance metrics
- Set reasonable performance thresholds
- Use conditional execution for performance tests
- Monitor memory usage and cleanup

## Advanced Testing Patterns

### 7. Parameterized Testing Pattern

**Purpose**: Test multiple scenarios with different parameters

```python
@pytest.mark.parametrize("provider,api_key_env,expected", [
    ("openai", "OPENAI_API_KEY", LLMProvider.OPENAI),
    ("fireworks", "FIREWORKS_API_KEY", LLMProvider.FIREWORKS),
    ("anthropic", "ANTHROPIC_API_KEY", LLMProvider.ANTHROPIC),
    ("gemini", "GEMINI_API_KEY", LLMProvider.GEMINI),
])
def test_provider_configuration_pattern(provider, api_key_env, expected, clean_env):
    """Pattern for testing multiple provider configurations."""
    test_config = {
        'CHAT_PROVIDER': provider,
        api_key_env: 'test-api-key-value'
    }
    
    with patch.dict(os.environ, test_config):
        from faultmaven.config.settings import get_settings
        
        settings = get_settings()
        assert settings.llm.provider == expected
        
        # Test provider-specific configuration
        if provider == "openai":
            assert settings.llm.openai_api_key == 'test-api-key-value'
        elif provider == "fireworks":
            assert settings.llm.fireworks_api_key == 'test-api-key-value'
        # ... etc for other providers

@pytest.mark.parametrize("error_type,expected_behavior", [
    (ConnectionError, "fallback_provider"),
    (TimeoutError, "retry_mechanism"),
    (ValueError, "error_response"),
])
def test_error_scenarios_pattern(error_type, expected_behavior):
    """Pattern for testing multiple error scenarios."""
    # Test different error conditions and expected behaviors
    pass
```

**Key Principles**:
- Use parameters for testing similar scenarios with different inputs
- Keep parameter lists manageable and meaningful
- Document what each parameter represents
- Consider using fixtures for complex parameter setup

### 8. Async Testing Pattern

**Purpose**: Proper async testing with container and interface mocks

```python
@pytest.mark.asyncio
async def test_async_pattern():
    """Pattern for async testing with proper mock setup."""
    from faultmaven.container import container
    from faultmaven.models.interfaces import ILLMProvider, IVectorStore
    
    container.reset()
    
    # Setup async mocks
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(return_value="Async LLM response")
    
    mock_vector_store = Mock(spec=IVectorStore)
    mock_vector_store.search = AsyncMock(return_value=[
        {"content": "Mock search result 1", "score": 0.95},
        {"content": "Mock search result 2", "score": 0.87}
    ])
    
    # Inject async mocks
    container._llm_provider = mock_llm
    container._vector_store = mock_vector_store
    
    # Test async operations
    knowledge_service = container.get_knowledge_service()
    agent_service = container.get_agent_service()
    
    # Concurrent async operations
    kb_task = knowledge_service.search_knowledge_base("test query")
    agent_task = agent_service.process_query("test query", "async-session")
    
    # Wait for both operations
    kb_results, agent_result = await asyncio.gather(kb_task, agent_task)
    
    # Validate async results
    assert len(kb_results) == 2
    assert agent_result.session_id == "async-session"
    
    # Validate async mock calls
    mock_vector_store.search.assert_called_once()
    mock_llm.generate_response.assert_called_once()
```

**Key Principles**:
- Use `@pytest.mark.asyncio` for async tests
- Use `AsyncMock` for async interface methods
- Test concurrent operations with `asyncio.gather()`
- Validate both results and mock interactions

### 9. Configuration Testing Pattern

**Purpose**: Test complex configuration scenarios

```python
def test_configuration_pattern():
    """Pattern for testing complex configuration scenarios."""
    # Test 1: Development configuration
    dev_config = {
        'ENVIRONMENT': 'development',
        'DEBUG': 'true',
        'LOG_LEVEL': 'DEBUG',
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-dev-key'
    }
    
    with patch.dict(os.environ, dev_config):
        settings = get_settings()
        assert settings.environment == Environment.DEVELOPMENT
        assert settings.debug is True
        assert settings.logging.level == LogLevel.DEBUG
    
    # Test 2: Production configuration
    prod_config = {
        'ENVIRONMENT': 'production',
        'DEBUG': 'false',
        'LOG_LEVEL': 'INFO',
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'fw-prod-key',
        'REDIS_HOST': '192.168.0.111',
        'REDIS_PORT': '30379',
        'CHROMADB_URL': 'http://chromadb.faultmaven.local:30080'
    }
    
    with patch.dict(os.environ, prod_config):
        settings = get_settings()
        assert settings.environment == Environment.PRODUCTION
        assert settings.debug is False
        assert settings.logging.level == LogLevel.INFO
        assert settings.database.redis_host == '192.168.0.111'
    
    # Test 3: Invalid configuration handling
    invalid_config = {
        'CHAT_PROVIDER': 'invalid_provider',
        'REDIS_PORT': 'not_a_number'
    }
    
    with patch.dict(os.environ, invalid_config):
        with pytest.raises(ValidationError) as exc_info:
            get_settings()
        
        assert "invalid_provider" in str(exc_info.value)
```

**Key Principles**:
- Test realistic configuration scenarios
- Test both valid and invalid configurations
- Use separate environment contexts for each test
- Validate all aspects of configuration processing

### 10. Integration Test Pattern with Mock Services

**Purpose**: Integration testing with controlled external service behavior

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_with_mock_services():
    """Pattern for integration testing with mock external services."""
    from tests.integration.mock_servers import MockLLMServer, MockVectorStore
    
    # Start mock external services
    async with MockLLMServer() as llm_server:
        async with MockVectorStore() as vector_store:
            
            # Configure container to use mock services
            container.reset()
            
            test_config = {
                'CHAT_PROVIDER': 'openai',
                'OPENAI_API_URL': llm_server.url,
                'CHROMADB_URL': vector_store.url,
                'OPENAI_API_KEY': 'mock-key'
            }
            
            with patch.dict(os.environ, test_config):
                # Test complete integration workflow
                agent_service = container.get_agent_service()
                
                # This will use mock external services
                result = await agent_service.process_query(
                    "Integration test query",
                    "integration-session"
                )
                
                # Validate integration results
                assert result.session_id == "integration-session"
                assert len(result.response) > 50
                
                # Validate mock service interactions
                assert llm_server.request_count > 0
                assert vector_store.search_count > 0
```

**Key Principles**:
- Use mock servers for external service simulation
- Test realistic integration scenarios
- Validate both application behavior and service interactions
- Use async context managers for proper cleanup

## Testing Utilities and Fixtures

### Common Test Fixtures

```python
# tests/conftest.py

@pytest.fixture(autouse=True)
def reset_container():
    """Automatically reset container before each test."""
    from faultmaven.container import container
    container.reset()
    yield
    container.reset()

@pytest.fixture
def clean_env():
    """Provide clean environment for testing."""
    original_env = os.environ.copy()
    # Clear FaultMaven environment variables
    for key in list(os.environ.keys()):
        if key.startswith(('CHAT_', 'REDIS_', 'CHROMADB_', 'LLM_')):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(original_env)

@pytest.fixture
def mock_dependencies():
    """Provide common mock dependencies."""
    from faultmaven.container import container
    from faultmaven.models.interfaces import ILLMProvider, ISanitizer
    
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(return_value="Mock response")
    
    mock_sanitizer = Mock(spec=ISanitizer)
    mock_sanitizer.sanitize = Mock(return_value="sanitized")
    
    container._llm_provider = mock_llm
    container._sanitizer = mock_sanitizer
    
    return {'llm': mock_llm, 'sanitizer': mock_sanitizer}

@pytest.fixture
def sample_settings_env():
    """Provide sample environment variables for settings testing."""
    return {
        'ENVIRONMENT': 'development',
        'DEBUG': 'true',
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-test-key',
        'REDIS_HOST': 'localhost',
        'REDIS_PORT': '6379'
    }
```

### Test Helper Functions

```python
# tests/utils/helpers.py

def create_mock_llm_provider(response="Mock LLM response"):
    """Create a mock LLM provider with standard response."""
    from faultmaven.models.interfaces import ILLMProvider
    
    mock = Mock(spec=ILLMProvider)
    mock.generate_response = AsyncMock(return_value=response)
    mock.name = "mock_provider"
    return mock

def create_mock_sanitizer(sanitized_text="sanitized input"):
    """Create a mock sanitizer with standard behavior."""
    from faultmaven.models.interfaces import ISanitizer
    
    mock = Mock(spec=ISanitizer)
    mock.sanitize = Mock(return_value=sanitized_text)
    mock.is_sensitive = Mock(return_value=False)
    return mock

async def create_test_session_result(session_id="test-session", response="Test response"):
    """Create a test session result for validation."""
    from faultmaven.models.api import QueryResponse
    
    return QueryResponse(
        session_id=session_id,
        response=response,
        confidence_score=0.95,
        metadata={"test": "true"}
    )

def assert_container_state_clean():
    """Assert that container is in clean state."""
    from faultmaven.container import container
    
    assert container._agent_service is None
    assert container._knowledge_service is None
    assert container._llm_provider is None
    assert container._sanitizer is None
```

## Best Practices Summary

### Testing Pattern Checklist

- ✅ **Container Reset**: Always call `container.reset()` before test logic
- ✅ **Interface Mocking**: Use `Mock(spec=Interface)` for dependency mocking  
- ✅ **Environment Isolation**: Use clean environment fixtures
- ✅ **Async Testing**: Use `AsyncMock` for async interface methods
- ✅ **Error Testing**: Test both success and failure scenarios
- ✅ **Performance Testing**: Monitor container and service performance
- ✅ **Integration Testing**: Test cross-layer workflows
- ✅ **Resource Management**: Ensure proper cleanup and isolation

### Common Anti-Patterns to Avoid

❌ **Direct Service Instantiation**: Don't create services directly  
✅ **Container Resolution**: Use `container.get_*_service()` methods

❌ **Implementation Mocking**: Don't mock concrete classes  
✅ **Interface Mocking**: Mock interfaces like `ILLMProvider`

❌ **Persistent State**: Don't let tests affect each other  
✅ **Clean State**: Reset container and environment between tests

❌ **Synchronous Async**: Don't use regular `Mock` for async methods  
✅ **Async Mocking**: Use `AsyncMock` for async interface methods

❌ **Environment Pollution**: Don't leave environment variables set  
✅ **Environment Cleanup**: Use fixtures for environment management

## Conclusion

These modern testing patterns provide a comprehensive foundation for testing FaultMaven's Clean Architecture implementation. The patterns support the **1425+ tests** across all architectural layers while maintaining proper isolation, realistic testing scenarios, and container-based dependency management.

Key benefits of these patterns:
- **Clean State Management**: Proper container reset and environment isolation
- **Interface Compliance**: All mocks implement proper interface contracts
- **Realistic Testing**: Focus on business logic validation rather than mock verification
- **Performance Awareness**: Monitor container overhead and test execution performance
- **Error Handling**: Comprehensive error scenario coverage and graceful degradation testing
- **Cross-Layer Integration**: End-to-end workflow validation through all architectural layers

By following these patterns, developers can ensure that all new tests maintain the same high quality and architectural compliance as the existing comprehensive test suite.