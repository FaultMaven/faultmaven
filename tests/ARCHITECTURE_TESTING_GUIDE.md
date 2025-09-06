# Comprehensive Architecture Testing Guide

**Document Type**: Architecture Testing Guide  
**Last Updated**: August 2025  
**Context**: Post-Architecture Overhaul - Clean Architecture with Dependency Injection

## Overview

This guide provides comprehensive testing strategies for FaultMaven's Clean Architecture implementation with dependency injection container patterns. With **1425+ tests** across architectural layers, this guide covers the **4 new comprehensive test files** and modern testing patterns developed after the major test architecture overhaul.

## Architecture Testing Philosophy

### Clean Architecture Testing Principles

FaultMaven's testing architecture follows Clean Architecture principles with these core tenets:

1. **Layer Isolation**: Tests are organized by architectural layer (API, Service, Core, Infrastructure)
2. **Dependency Direction**: Dependencies always point inward toward the core domain
3. **Interface Contracts**: All dependencies are injected through interfaces, not concrete implementations
4. **Container-Based Resolution**: All services are resolved through the dependency injection container
5. **Mock External Boundaries**: Only mock at system boundaries (external APIs, databases, file systems)

### Container-Based Testing Strategy

All tests use the FaultMaven dependency injection container for:

- **Service Resolution**: All services obtained through `container.get_*_service()` methods
- **Clean State Management**: `container.reset()` ensures isolation between tests
- **Interface Injection**: Dependencies injected as interfaces for proper abstraction
- **Mock Integration**: Test-specific mocks integrated through container patterns
- **Lifecycle Management**: Proper initialization and cleanup of container services

## New Comprehensive Test Architecture

### 1. Settings System Testing (`test_settings_system_comprehensive.py`)

**Coverage**: 37+ tests across 10 test classes  
**Focus**: Complete replacement of legacy configuration system

#### Test Classes Overview

```python
class TestServerSettings:
    """Server configuration validation (7 tests)"""
    def test_default_server_configuration(self)
    def test_custom_host_port_configuration(self)
    def test_debug_mode_configuration(self)
    def test_environment_detection(self)
    def test_cors_configuration_parsing(self)
    def test_invalid_port_handling(self)
    def test_server_settings_integration_with_environment(self)

class TestLLMSettings:
    """LLM provider configuration (4 tests)"""
    def test_default_llm_configuration(self)
    def test_provider_specific_configuration(self)
    def test_api_key_security_and_masking(self)
    def test_model_selection_validation(self)

class TestFaultMavenSettings:
    """Main settings class integration (8 tests)"""
    def test_default_settings_initialization(self)
    def test_environment_variable_override(self)
    def test_nested_settings_integration(self)
    def test_settings_validation_with_invalid_data(self)
    def test_production_vs_development_settings(self)
    def test_cors_and_redis_url_generation(self)
    def test_comprehensive_environment_processing(self)
    def test_settings_serialization_and_deserialization(self)
```

#### Key Testing Patterns

**Environment Isolation Pattern**:
```python
@pytest.fixture
def clean_env():
    """Provide clean environment for settings testing."""
    original_env = os.environ.copy()
    # Clear all FaultMaven-related environment variables
    for key in list(os.environ.keys()):
        if any(prefix in key for prefix in ['CHAT_', 'REDIS_', 'CHROMADB_']):
            del os.environ[key]
    
    yield
    
    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)

def test_production_configuration(clean_env):
    """Test production-like configuration scenarios."""
    production_env = {
        'ENVIRONMENT': 'production',
        'DEBUG': 'false',
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'fw-prod-key',
        'REDIS_HOST': '192.168.0.111',
        'REDIS_PORT': '30379'
    }
    
    with patch.dict(os.environ, production_env):
        settings = get_settings()
        assert settings.environment == Environment.PRODUCTION
        assert not settings.debug
        assert settings.llm.provider == LLMProvider.FIREWORKS
        assert settings.database.redis_host == '192.168.0.111'
```

### 2. LLM Registry Testing (`test_llm_registry_comprehensive.py`)

**Coverage**: 37+ tests across 7 test classes  
**Focus**: Centralized 7-provider management with fallback chains

#### Test Classes Overview

```python
class TestProviderRegistryInitialization:
    """Registry initialization patterns (4 tests)"""
    def test_singleton_initialization(self)
    def test_lazy_provider_loading(self)
    def test_registry_reset_isolation(self)
    def test_concurrent_initialization_safety(self)

class TestProviderConfiguration:
    """Schema-driven configuration (8 tests)"""
    def test_provider_schema_validation(self)
    def test_environment_based_provider_registration(self)
    def test_provider_priority_ordering(self)
    def test_api_key_based_provider_activation(self)
    def test_model_configuration_validation(self)
    def test_provider_capability_detection(self)
    def test_invalid_provider_configuration_handling(self)
    def test_provider_schema_completeness_validation(self)

class TestFallbackChain:
    """Multi-provider fallback behavior (5 tests)"""
    def test_primary_provider_selection(self)
    def test_automatic_fallback_on_failure(self)
    def test_fallback_chain_ordering(self)
    def test_fallback_exhaustion_handling(self)
    def test_provider_recovery_after_failure(self)
```

#### Key Testing Patterns

**Provider Registry Pattern**:
```python
def test_provider_fallback_chain():
    """Test multi-provider fallback behavior."""
    registry = get_provider_registry()
    registry.reset()  # Clean state
    
    # Configure multiple providers
    with patch.dict(os.environ, {
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-test',
        'FIREWORKS_API_KEY': 'fw-test',
        'ANTHROPIC_API_KEY': 'claude-test'
    }):
        # Test primary provider selection
        primary = registry.get_primary_provider()
        assert primary.name == 'openai'
        
        # Test fallback chain
        fallback_chain = registry.get_fallback_chain()
        assert len(fallback_chain) >= 2
        assert 'fireworks' in [p.name for p in fallback_chain]
        
        # Test provider availability
        available = registry.get_available_providers()
        assert len(available) == 3  # openai, fireworks, anthropic
```

### 3. Container Integration Testing (`test_container_integration_comprehensive.py`)

**Coverage**: 38+ tests across 9 test classes  
**Focus**: Complete dependency injection container system

#### Test Classes Overview

```python
class TestContainerSingleton:
    """Singleton pattern validation (4 tests)"""
    def test_container_singleton_instance(self)
    def test_container_state_persistence(self)
    def test_container_reset_cleanup(self)
    def test_container_thread_safety(self)

class TestServiceLifecycleManagement:
    """Service creation and lifecycle (4 tests)"""
    def test_service_lazy_initialization(self)
    def test_service_singleton_behavior(self)
    def test_service_dependency_resolution(self)
    def test_service_cleanup_on_container_reset(self)

class TestInterfaceResolutionAndInjection:
    """Dependency injection patterns (4 tests)"""
    def test_interface_to_implementation_mapping(self)
    def test_dependency_injection_chain(self)
    def test_circular_dependency_detection(self)
    def test_interface_compliance_validation(self)
```

#### Key Testing Patterns

**Container Lifecycle Pattern**:
```python
def test_service_lifecycle_management():
    """Test complete service lifecycle through container."""
    from faultmaven.container import container
    
    # Reset for clean state
    container.reset()
    assert container._agent_service is None
    
    # Test lazy initialization
    agent_service_1 = container.get_agent_service()
    assert agent_service_1 is not None
    assert container._agent_service is agent_service_1
    
    # Test singleton behavior
    agent_service_2 = container.get_agent_service()
    assert agent_service_2 is agent_service_1  # Same instance
    
    # Test dependency injection
    llm_provider = container.get_llm_provider()
    sanitizer = container.get_sanitizer()
    
    assert llm_provider is not None
    assert sanitizer is not None
    
    # Test container reset cleanup
    container.reset()
    assert container._agent_service is None
    assert container._llm_provider is None
```

### 4. Architecture Workflow Testing (`test_new_architecture_workflows.py`)

**Coverage**: 18+ tests across 5 test classes  
**Focus**: End-to-end integration workflows

#### Test Classes Overview

```python
class TestSettingsContainerServicesFlow:
    """Settings → Container → Services flow (4 tests)"""
    def test_environment_to_settings_integration(self)
    def test_settings_to_container_initialization(self)
    def test_container_to_service_resolution(self)
    def test_end_to_end_configuration_flow(self)

class TestEndToEndWorkflows:
    """Complete troubleshooting workflows (4 tests)"""
    def test_complete_troubleshooting_workflow(self)
    def test_data_upload_and_analysis_integration(self)
    def test_knowledge_base_integration_workflow(self)
    def test_multi_session_case_continuity(self)

class TestErrorHandlingAcrossLayers:
    """Cross-layer error propagation (4 tests)"""
    def test_api_to_service_error_propagation(self)
    def test_service_to_infrastructure_error_handling(self)
    def test_graceful_degradation_scenarios(self)
    def test_error_recovery_and_fallback_mechanisms(self)
```

#### Key Testing Patterns

**End-to-End Workflow Pattern**:
```python
@pytest.mark.asyncio
async def test_complete_troubleshooting_workflow():
    """Test complete workflow through all architectural layers."""
    from faultmaven.container import container
    from faultmaven.config.settings import get_settings
    
    # Reset for clean state
    container.reset()
    
    # Test configuration layer
    test_env = {
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'test-key',
        'REDIS_HOST': 'localhost'
    }
    
    with patch.dict(os.environ, test_env):
        # Validate settings layer
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.FIREWORKS
        
        # Validate container initialization
        agent_service = container.get_agent_service()
        assert agent_service is not None
        
        # Test service layer operation
        result = await agent_service.process_query(
            "Server returning 500 errors consistently",
            "workflow-test-session"
        )
        
        # Validate complete workflow results
        assert result.session_id == "workflow-test-session"
        assert len(result.response) > 100
        assert any(keyword in result.response.lower() 
                  for keyword in ['server', '500', 'error'])
        
        # Validate context propagation
        assert hasattr(result, 'metadata')
        assert result.metadata is not None
```

## Testing Patterns by Architectural Layer

### API Layer Testing

**Focus**: FastAPI endpoint validation with proper dependency injection

```python
@pytest.mark.asyncio
async def test_api_endpoint_with_container(async_client):
    """Test API endpoint using container-injected services."""
    from faultmaven.container import container
    
    # Ensure clean container state
    container.reset()
    
    # Test API endpoint
    response = await async_client.post(
        "/agent/query",
        json={
            "query": "Database connection issues",
            "session_id": "api-test-session"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "api-test-session"
    assert len(data["response"]) > 50
```

### Service Layer Testing

**Focus**: Business logic orchestration with injected dependencies

```python
@pytest.mark.asyncio
async def test_service_with_mock_dependencies():
    """Test service layer with properly mocked dependencies."""
    from faultmaven.container import container
    from faultmaven.models.interfaces import ILLMProvider, ISanitizer
    
    # Reset container and inject mocks
    container.reset()
    
    # Create interface-compliant mocks
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(return_value="Mock response")
    
    mock_sanitizer = Mock(spec=ISanitizer)
    mock_sanitizer.sanitize = Mock(return_value="sanitized query")
    
    # Inject mocks through container
    container._llm_provider = mock_llm
    container._sanitizer = mock_sanitizer
    
    # Test service operation
    agent_service = container.get_agent_service()
    result = await agent_service.process_query(
        "Test query", 
        "service-test-session"
    )
    
    # Validate service behavior
    assert result.session_id == "service-test-session"
    mock_sanitizer.sanitize.assert_called_once()
    mock_llm.generate_response.assert_called_once()
```

### Core Domain Testing

**Focus**: Business logic validation with minimal mocking

```python
def test_core_domain_logic():
    """Test core domain logic without external dependencies."""
    from faultmaven.core.processing.classifier import DataClassifier
    
    classifier = DataClassifier()
    
    # Test with realistic data
    system_log = "2024-08-28 ERROR: Database connection timeout"
    result = classifier.classify(system_log)
    
    assert result == DataType.SYSTEM_LOGS
    assert classifier.confidence_score > 0.8
```

### Infrastructure Layer Testing

**Focus**: External service integration with proper fallbacks

```python
@pytest.mark.asyncio
async def test_infrastructure_with_fallback():
    """Test infrastructure layer with fallback behavior."""
    from faultmaven.infrastructure.llm.router import LLMRouter
    
    router = LLMRouter()
    
    # Test with realistic provider configuration
    with patch.dict(os.environ, {
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'test-key',
        'FIREWORKS_API_KEY': 'fallback-key'
    }):
        # Test primary provider
        response = await router.route("Test query")
        assert len(response) > 50
        
        # Test fallback behavior (simulate primary failure)
        with patch.object(router.primary_provider, 'generate_response', 
                         side_effect=Exception("Primary failed")):
            fallback_response = await router.route("Test query")
            assert len(fallback_response) > 50  # Fallback worked
```

## Advanced Testing Scenarios

### Cross-Layer Error Handling

```python
@pytest.mark.asyncio
async def test_error_propagation_across_layers():
    """Test error handling and recovery across all layers."""
    from faultmaven.container import container
    from faultmaven.models.interfaces import ILLMProvider
    
    container.reset()
    
    # Create mock that raises exception
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(
        side_effect=Exception("LLM service unavailable")
    )
    
    container._llm_provider = mock_llm
    
    # Test error propagation through service layer
    agent_service = container.get_agent_service()
    
    with pytest.raises(Exception) as exc_info:
        await agent_service.process_query("Test", "error-session")
    
    # Validate proper error handling
    assert "LLM service unavailable" in str(exc_info.value)
    
    # Test graceful degradation if implemented
    # (Service should handle errors gracefully in production)
```

### Performance and Resource Management

```python
def test_container_performance_overhead():
    """Test container performance and resource usage."""
    from faultmaven.container import container
    import time
    
    # Measure container reset performance
    start_time = time.time()
    for _ in range(100):
        container.reset()
        agent_service = container.get_agent_service()
        assert agent_service is not None
    
    elapsed_time = time.time() - start_time
    
    # Container operations should be fast
    assert elapsed_time < 1.0  # Less than 1 second for 100 operations
    assert elapsed_time / 100 < 0.01  # Less than 10ms per operation
```

### Integration Testing with External Services

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_external_service_integration():
    """Test integration with external services when available."""
    from faultmaven.container import container
    
    # Skip if external services not available
    if os.getenv('SKIP_SERVICE_CHECKS', 'false').lower() == 'true':
        pytest.skip("External service checks disabled")
    
    container.reset()
    
    # Test with real external services if available
    try:
        agent_service = container.get_agent_service()
        result = await agent_service.process_query(
            "Simple test query",
            "external-integration-session"
        )
        
        assert result.session_id == "external-integration-session"
        assert len(result.response) > 20
        
    except Exception as e:
        # Log but don't fail if external services unavailable
        pytest.skip(f"External services unavailable: {e}")
```

## Best Practices for Architecture Testing

### 1. Container State Management

- **Always reset**: Use `container.reset()` before each test
- **Clean isolation**: Ensure tests don't interfere with each other
- **Proper cleanup**: Container handles service lifecycle management
- **Thread safety**: Container is thread-safe for concurrent testing

### 2. Interface-Based Mocking

- **Mock interfaces**: Mock `ILLMProvider`, not `OpenAIProvider`
- **Implement properly**: Use `Mock(spec=Interface)` for compliance
- **Realistic behavior**: Mocks should behave like real implementations
- **Error scenarios**: Test both success and failure paths

### 3. Environment Management

- **Clean environment**: Use fixtures to provide isolated environments
- **Realistic configuration**: Test with production-like settings
- **Variable validation**: Test all environment variable scenarios
- **Error handling**: Test invalid configuration handling

### 4. Performance Considerations

- **Container overhead**: Monitor container operation performance
- **Test execution time**: Keep tests fast with proper mocking
- **Resource usage**: Ensure proper cleanup and resource management
- **Concurrent testing**: Validate thread safety and isolation

### 5. Error Scenario Coverage

- **Cross-layer errors**: Test error propagation between layers
- **Graceful degradation**: Test fallback and recovery mechanisms
- **Edge cases**: Test boundary conditions and invalid inputs
- **Recovery testing**: Test system recovery after failures

## Testing Tools and Utilities

### Container Testing Utilities

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
def mock_container_dependencies():
    """Provide common mock dependencies for container testing."""
    from faultmaven.container import container
    from faultmaven.models.interfaces import ILLMProvider, ISanitizer
    
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response = AsyncMock(return_value="Mock response")
    
    mock_sanitizer = Mock(spec=ISanitizer)
    mock_sanitizer.sanitize = Mock(return_value="sanitized")
    
    container._llm_provider = mock_llm
    container._sanitizer = mock_sanitizer
    
    return {
        'llm_provider': mock_llm,
        'sanitizer': mock_sanitizer
    }
```

### Environment Testing Utilities

```python
# tests/utils/environment.py
@contextmanager
def test_environment(**env_vars):
    """Context manager for temporary environment variables."""
    original_env = os.environ.copy()
    try:
        os.environ.update(env_vars)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original_env)

# Usage
def test_with_custom_environment():
    with test_environment(
        CHAT_PROVIDER='fireworks',
        FIREWORKS_API_KEY='test-key'
    ):
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.FIREWORKS
```

## Troubleshooting Architecture Tests

### Common Issues and Solutions

**Container State Issues**:
```python
# Problem: Tests interfere with each other
# Solution: Proper container reset
def test_example():
    container.reset()  # Clean state
    # ... test code
```

**Interface Mock Issues**:
```python
# Problem: Mock doesn't implement interface properly
# Solution: Use spec parameter
mock_service = Mock(spec=IServiceInterface)
mock_service.method_name = Mock(return_value="value")
```

**Environment Variable Issues**:
```python
# Problem: Environment variables persist between tests
# Solution: Use clean environment fixture
def test_with_clean_env(clean_env):
    os.environ['TEST_VAR'] = 'value'
    # Test code
    # Environment automatically restored
```

**Async Testing Issues**:
```python
# Problem: Async mocks not working properly
# Solution: Use AsyncMock for async methods
mock_service.async_method = AsyncMock(return_value="result")
```

## Conclusion

This architecture testing guide provides comprehensive patterns for testing FaultMaven's Clean Architecture implementation with dependency injection. The **1425+ tests** across **4 new comprehensive test files** ensure robust validation of all architectural components while maintaining proper isolation and realistic testing scenarios.

Key takeaways:
- Always use container-based patterns for service resolution
- Mock interfaces, not implementations
- Maintain clean state with proper container reset
- Test cross-layer integration with realistic scenarios
- Validate error handling and graceful degradation
- Monitor performance and resource usage

The architecture testing approach ensures that FaultMaven's Clean Architecture principles are properly validated while maintaining test reliability and maintainability.