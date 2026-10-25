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

## Comprehensive Test Architecture

### 1. LLM Registry Testing (`test_llm_registry_comprehensive.py`)

**Coverage**: 52 tests across 8 test classes (`tests/infrastructure/`)
**Focus**: Centralized 9-provider management with fallback chains

#### Test Classes Overview

```python
class TestProviderRegistryInitialization:
    """Registry initialization patterns (5 tests)"""
    def test_registry_singleton(self)
    def test_registry_reset(self)
    def test_lazy_initialization(self)
    def test_initialization_without_settings(self)
    def test_initialization_with_mock_settings(self, clean_env)

class TestProviderConfiguration:
    """Schema-driven configuration (11 tests)"""
    def test_provider_schema_completeness(self)
    def test_get_valid_provider_names(self)
    def test_provider_config_creation_with_api_key(self)
    def test_provider_config_creation_without_api_key(self)
    def test_local_provider_config_creation(self)
    def test_local_provider_missing_requirements(self)
    def test_per_provider_override_takes_precedence_over_global_default(self)
    def test_schema_timeout_wins_over_provider_override(self)
    def test_no_override_falls_through_to_global_default(self)
    def test_provider_initialization_success(self, clean_env, mock_provider_classes)
    def test_provider_initialization_failure(self, clean_env)

class TestFallbackChain:
    """Multi-provider fallback behavior (10 tests)"""
    def test_fallback_chain_setup_primary_available(self, mock_provider_classes)
    def test_fallback_chain_setup_primary_unavailable(self, clean_env, mock_provider_classes)
    def test_invalid_primary_provider_fallback(self, clean_env, sample_env_vars, mock_provider_classes)
    async def test_route_request_success_first_provider(self, clean_env, sample_env_vars, mock_provider_classes)
    async def test_route_request_fallback_on_failure(self, clean_env, sample_env_vars, mock_provider_classes)
    async def test_route_request_all_providers_fail(self, mock_provider_classes)
    async def test_route_request_confidence_threshold(self, clean_env, sample_env_vars, mock_provider_classes)
    async def test_route_request_skips_confidence_check_when_tools_present(self, clean_env, sample_env_vars, mock_provider_classes)
    async def test_route_request_without_tools_still_filters_by_confidence(self, clean_env, sample_env_vars, mock_provider_classes)
    async def test_route_request_best_low_confidence_includes_zero(self, clean_env, sample_env_vars, mock_provider_classes)
```

#### Key Testing Patterns

**Provider Registry Pattern**:
```python
def test_provider_fallback_chain():
    """Test multi-provider fallback behavior."""
    from faultmaven.config.settings import reset_settings
    from faultmaven.infrastructure.llm.providers.registry import (
        get_registry,
        reset_registry,
    )

    # Configure multiple providers
    with patch.dict(os.environ, {
        'CHAT_PROVIDER': 'openai',
        'STRICT_PROVIDER_MODE': 'false',  # the default is the primary alone
        'OPENAI_API_KEY': 'sk-test',
        'FIREWORKS_API_KEY': 'fw-test',
        'ANTHROPIC_API_KEY': 'claude-test'
    }):
        # Clean state: the registry reads the settings when it is built
        reset_settings()
        reset_registry()
        registry = get_registry()

        # Test primary provider selection: the chain starts with it
        fallback_chain = registry.get_fallback_chain()
        assert fallback_chain[0] == 'openai'

        # Test fallback chain (provider names)
        assert len(fallback_chain) >= 2
        assert 'fireworks' in fallback_chain

        # Test provider availability
        available = registry.get_available_providers()
        assert {'openai', 'fireworks', 'anthropic'} <= set(available)

    reset_registry()
    reset_settings()
```

### 2. Container Integration Testing (`test_container_integration_comprehensive.py`)

**Coverage**: 18 tests across 8 test classes (`tests/unit/`)
**Focus**: Complete dependency injection container system

#### Test Classes Overview

```python
class TestContainerSingleton:
    """Singleton pattern validation (2 tests)"""
    def test_container_singleton_pattern(self)
    def test_container_reset_creates_new_instance(self)

class TestServiceLifecycleManagement:
    """Service creation and lifecycle (1 test)"""
    async def test_graceful_degradation_without_interfaces(self, clean_env)

class TestInterfaceResolutionAndInjection:
    """Dependency injection patterns (3 tests)"""
    def test_service_getter_methods(self, clean_env, mock_services)
    def test_service_getter_never_initializes(self, clean_env)
    def test_infrastructure_provider_getters(self, clean_env, mock_services)
```

#### Key Testing Patterns

**Container Lifecycle Pattern**:
```python
async def test_service_lifecycle_management():
    """Test complete service lifecycle through container."""
    from faultmaven.container import container

    # Reset for clean state, so the composition below is this loop's
    container.reset()

    # A getter never composes the container (#2249): initialize it first, and
    # shut it down on the loop it was initialized on.
    await container.initialize()
    try:
        agent_service_1 = container.get_agent_service()
        assert agent_service_1 is not None

        # Test singleton behavior
        assert container.get_agent_service() is agent_service_1  # Same instance

        # Test dependency injection
        assert container.get_llm_provider() is not None
        assert container.get_sanitizer() is not None
    finally:
        # shutdown() closes what initialize() opened and resets the container
        await container.shutdown()

    assert container.get_agent_service() is None
```

## Testing Patterns by Architectural Layer

### API Layer Testing

**Focus**: FastAPI endpoint validation with proper dependency injection

```python
def test_api_endpoint_with_container(booted_app_client):
    """Test API endpoint using container-injected services."""
    # booted_app_client (tests/conftest.py) is a TestClient over the started
    # app: its lifespan composed the container. Sign in as the local admin.
    login = booted_app_client.post(
        "/api/v1/auth/dev-login", json={"username": "admin"}
    )
    token = login.json()["access_token"]

    # Test API endpoint
    response = booted_app_client.post(
        "/api/v1/cases",
        json={"title": "Database connection issues"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Database connection issues"
    assert data["state"] == "inquiry"
```

### Service Layer Testing

**Focus**: Business logic orchestration with injected dependencies

```python
@pytest.mark.asyncio
async def test_service_with_mock_dependencies():
    """Test service layer with properly mocked dependencies."""
    from unittest.mock import MagicMock, create_autospec

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from faultmaven.models.interfaces import IKnowledgeIngester, ITracer
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    # Create specced mocks
    vector_store = create_autospec(KnowledgeVectorStore, instance=True)
    vector_store.search.return_value = []
    sanitizer = create_autospec(DataSanitizer, instance=True)
    sanitizer.asanitize.return_value = "sanitized query"

    # Inject the mocks through the constructor
    knowledge_service = KnowledgeService(
        knowledge_ingester=create_autospec(IKnowledgeIngester, instance=True),
        sanitizer=sanitizer,
        tracer=create_autospec(ITracer, instance=True),
        vector_store=vector_store,
        db_session_factory=MagicMock(),  # required; a search never opens a session
    )

    # Test service operation
    results = await knowledge_service.search_knowledge("Test query")

    # Validate service behavior: the store sees the sanitized query
    assert results == []
    sanitizer.asanitize.assert_awaited_once_with("Test query")
    assert vector_store.search.await_args.kwargs["query"] == "sanitized query"
```

### Core Domain Testing

**Focus**: Business logic validation with minimal mocking

```python
def test_core_domain_logic():
    """Test core domain logic without external dependencies."""
    from faultmaven.models.api import DataType
    from faultmaven.modules.preprocessing.classifier import DataClassifier

    classifier = DataClassifier()

    # Test with realistic data
    system_log = "2024-08-28 10:00:01 ERROR Database connection timeout"
    result = classifier.classify("app.log", system_log)

    assert result.data_type == DataType.LOGS_AND_ERRORS
    assert result.confidence > 0.8
```

### Infrastructure Layer Testing

**Focus**: External service integration with proper fallbacks

```python
@pytest.mark.asyncio
async def test_infrastructure_with_fallback():
    """Test infrastructure layer with fallback behavior."""
    from faultmaven.config.settings import reset_settings
    from faultmaven.infrastructure.llm.providers.base import LLMResponse
    from faultmaven.infrastructure.llm.providers.registry import (
        get_registry,
        reset_registry,
    )
    from tests.model_content import vetted

    # Test with realistic provider configuration
    with patch.dict(os.environ, {
        'CHAT_PROVIDER': 'openai',
        'STRICT_PROVIDER_MODE': 'false',
        'OPENAI_API_KEY': 'test-key',
        'FIREWORKS_API_KEY': 'fallback-key'
    }):
        reset_settings()
        reset_registry()
        registry = get_registry()
        fallback_answer = LLMResponse(
            content="Fallback answer",
            confidence=0.9,
            provider="fireworks",
            model="test-model",
            tokens_used=10,
            response_time_ms=5,
        )

        # Test fallback behavior (simulate primary failure). Each provider's
        # send, _generate, is patched: no test reaches a provider
        with patch.object(registry.get_provider('openai'), '_generate',
                          side_effect=Exception("Primary failed")), \
             patch.object(registry.get_provider('fireworks'), '_generate',
                          return_value=fallback_answer):
            response = await registry.route_request(
                prompt=vetted("Test query"), max_tokens=100
            )
            assert response.content == "Fallback answer"  # Fallback worked

    reset_registry()
    reset_settings()
```

## Advanced Testing Scenarios

### Cross-Layer Error Handling

```python
@pytest.mark.asyncio
async def test_error_propagation_across_layers():
    """Test error handling and recovery across all layers."""
    from unittest.mock import MagicMock, create_autospec

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from faultmaven.models.interfaces import IKnowledgeIngester, ITracer
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    # Create mock that raises exception
    vector_store = create_autospec(KnowledgeVectorStore, instance=True)
    vector_store.search.side_effect = Exception("Vector store unavailable")
    sanitizer = create_autospec(DataSanitizer, instance=True)
    sanitizer.asanitize.side_effect = lambda data: data

    knowledge_service = KnowledgeService(
        knowledge_ingester=create_autospec(IKnowledgeIngester, instance=True),
        sanitizer=sanitizer,
        tracer=create_autospec(ITracer, instance=True),
        vector_store=vector_store,
        db_session_factory=MagicMock(),
    )

    # Test error propagation through service layer
    with pytest.raises(Exception) as exc_info:
        await knowledge_service.search_knowledge("Test")

    # Validate proper error handling
    assert "Vector store unavailable" in str(exc_info.value)

    # Test graceful degradation: patterns.md, pattern 5
```

### Performance and Resource Management

```python
def test_container_performance_overhead():
    """Test container performance and resource usage."""
    from faultmaven.container import container
    import time

    # Measure container reset performance (a getter never composes the
    # container, #2249, so this is reset and lookup, not composition)
    start_time = time.time()
    for _ in range(100):
        container.reset()
        agent_service = container.get_agent_service()
        assert agent_service is None

    elapsed_time = time.time() - start_time

    # Container operations should be fast
    assert elapsed_time < 1.0  # Less than 1 second for 100 operations
    assert elapsed_time / 100 < 0.01  # Less than 10ms per operation
```

### Integration Testing with External Services

No test reaches an external service: `tests/network_guard.py` refuses every
connection a test did not start itself. The one exception is the opt-in
real-LLM lane (`@pytest.mark.real_llm`), described in
[standards.md](standards.md).

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
# tests/conftest.py: requested by name, not autouse
@pytest.fixture(scope="function")
async def reset_container():
    """Reset the DI container before each test, and shut it down after it."""
    from faultmaven.container import container

    container.reset()
    os.environ["SKIP_SERVICE_CHECKS"] = "true"

    yield container

    # Closes what an initialize() in the test opened, on the test's loop
    await container.shutdown()
    container.reset()
```

### Environment Testing Utilities

```python
# pytest's monkeypatch restores each variable it sets after the test
def test_with_custom_environment(monkeypatch):
    from faultmaven.config.settings import LLMProvider, get_settings, reset_settings

    monkeypatch.setenv('CHAT_PROVIDER', 'fireworks')
    monkeypatch.setenv('FIREWORKS_API_KEY', 'test-key')
    reset_settings()  # get_settings() caches the instance it built first
    try:
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.FIREWORKS
    finally:
        reset_settings()
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
# Solution: Spec it on the interface; a name the interface lacks raises
# AttributeError, and a call with the wrong arguments raises TypeError
from unittest.mock import create_autospec

from faultmaven.models.interfaces import ITracer

mock_tracer = create_autospec(ITracer, instance=True)
mock_tracer.trace("operation")  # ITracer declares trace(operation)
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
# Solution: Use AsyncMock for async methods; create_autospec makes one for each
from unittest.mock import create_autospec

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)

vector_store = create_autospec(KnowledgeVectorStore, instance=True)
vector_store.search.return_value = []  # search is an AsyncMock: await it
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
