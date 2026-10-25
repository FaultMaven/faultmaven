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

async def test_service_with_container_pattern():
    """Standard pattern for container-based testing."""
    # ALWAYS reset container for clean state
    container.reset()

    # Compose it: a getter never does (#2249)
    await container.initialize()
    try:
        # Get service through container (dependency injection)
        agent_service = container.get_agent_service()

        # Validate service is properly initialized
        assert agent_service is not None

        # Test service functionality
        # ... test code here
    finally:
        # Close what initialize() opened, on this loop, and reset
        await container.shutdown()
```

**Key Principles**:
- Always call `container.reset()` before test logic
- Use container methods for service resolution (`container.get_*_service()`)
- Let container manage service lifecycle and dependencies
- Use this pattern when the composition is what the test is about; a unit
  test builds its unit directly, with mocked dependencies (pattern 2)

### 2. Interface-Based Mocking Pattern

**Purpose**: Mock dependencies by the type the service is given, so a mock answers only what the real dependency would

```python
from unittest.mock import MagicMock, create_autospec

import pytest

from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models.interfaces import IKnowledgeIngester, ITracer
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

@pytest.mark.asyncio
async def test_service_with_interface_mocks():
    """Pattern for building the unit under test on mocked dependencies."""
    # Create specced mocks: a method the type lacks, or a call with the
    # wrong arguments, fails the test
    vector_store = create_autospec(KnowledgeVectorStore, instance=True)
    vector_store.search.return_value = [
        {
            "id": "kb_pool_chunk_0",
            "content": "Raise the connection pool size",
            "score": 0.92,
            "metadata": {"title": "Database pool exhaustion"},
        }
    ]

    sanitizer = create_autospec(DataSanitizer, instance=True)
    sanitizer.asanitize.side_effect = lambda data: data

    # Pass the mocks to the constructor of the unit under test
    service = KnowledgeService(
        knowledge_ingester=create_autospec(IKnowledgeIngester, instance=True),
        sanitizer=sanitizer,
        tracer=create_autospec(ITracer, instance=True),
        vector_store=vector_store,
        db_session_factory=MagicMock(),  # required; a search never opens a session
    )

    # Test service with mocked dependencies
    results = await service.search_knowledge("database timeout", limit=3)

    # Validate interactions
    sanitizer.asanitize.assert_awaited_once_with("database timeout")
    vector_store.search.assert_awaited_once_with(
        collection_name=KB_COLLECTION,
        query="database timeout",
        k=3,
        where={"scope": "global"},
    )

    # Validate results
    assert [r.title for r in results] == ["Database pool exhaustion"]
    assert results[0].parent_document_id == "kb_pool"
```

**Key Principles**:
- Use `create_autospec(Type, instance=True)`: it checks every call against the
  real signature. `Mock(spec=Type)` checks attribute names only, and a bare
  `AsyncMock` accepts any call, which is how a vector-store call with the wrong
  signature went unnoticed
  (`tests/unit/modules/knowledge/test_knowledge_service_vector_search_contract.py`)
- Spec on the interface in `faultmaven/models/interfaces.py` where it declares
  the method the service calls (`ITracer.trace`), otherwise on the class the
  container passes: `ISanitizer` declares no `asanitize`, and the knowledge
  vector store implements no interface
- `create_autospec` makes each async method an `AsyncMock`; check it with `assert_awaited_*`
- Pass mocks to the constructor; never assign them onto the container

### 3. Environment Isolation Pattern

**Purpose**: Provide clean, isolated environment configuration for each test

```python
import os
from unittest.mock import patch

import pytest

from faultmaven.config.settings import LLMProvider, get_settings, reset_settings

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
    # get_settings() caches the first instance it builds
    reset_settings()

    yield

    # Restore original environment, and drop the settings built from the test's
    os.environ.clear()
    os.environ.update(original_env)
    reset_settings()

def test_settings_with_environment_isolation(clean_env):
    """Test with isolated environment configuration."""
    # Set test-specific environment
    test_config = {
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'test-key-123',
        'REDIS_HOST': 'redis.test',
        'REDIS_PORT': '6380'
    }

    with patch.dict(os.environ, test_config):
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.FIREWORKS
        assert settings.llm.fireworks_api_key.get_secret_value() == 'test-key-123'
        assert settings.database.redis_host == 'redis.test'

    # Environment automatically restored by fixture
```

**Key Principles**:
- Use fixtures for environment isolation
- Clear relevant environment variables before test
- Use `patch.dict` for temporary environment changes
- Always restore original environment after test
- Reset the settings singleton (`reset_settings()`) on both sides: `get_settings()` caches

### 4. Cross-Layer Integration Pattern

**Purpose**: Test complete workflows through all architectural layers

```python
import os
from unittest.mock import patch

import pytest

@pytest.mark.asyncio
async def test_cross_layer_integration(reset_container):
    """Pattern for testing complete architectural workflows."""
    from faultmaven.config.settings import LLMProvider, get_settings, reset_settings

    # Step 1: The reset_container fixture (tests/conftest.py) resets the
    # container, and shuts it down after the test

    # Step 2: Configure environment (Settings layer)
    test_environment = {
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-test-key',
        'DEBUG': 'true'
    }

    with patch.dict(os.environ, test_environment):
        reset_settings()  # get_settings() caches the instance it built first
        try:
            # Step 3: Validate Settings layer
            settings = get_settings()
            assert settings.llm.provider == LLMProvider.OPENAI
            assert settings.server.debug is True

            # Step 4: Compose the container (a getter never does, #2249) and
            # validate the Container layer (dependency resolution)
            await reset_container.initialize()
            agent_service = reset_container.get_agent_service()
            knowledge_service = reset_container.get_knowledge_service()

            assert agent_service is not None
            assert knowledge_service is not None

            # Step 5: Test Service layer integration, down to the vector store
            # (the test process's own, empty knowledge base)
            kb_results = await knowledge_service.search_knowledge(
                "database connection issues",
                limit=3
            )
            assert kb_results == []
        finally:
            reset_settings()
```

**Key Principles**:
- Test workflows through the layers they cross, on the composed container
- Validate each layer's functionality independently
- Test layer interactions and data flow
- Use realistic test data and scenarios
- Leave `REDIS_HOST` as `tests/conftest.py` sets it (empty): the composition
  then uses the in-process FakeRedis. A host there is a connection
  `tests/network_guard.py` refuses

### 5. Error Handling and Graceful Degradation Pattern

**Purpose**: Test error propagation and recovery across architectural layers

```python
@pytest.mark.asyncio
async def test_error_handling_pattern():
    """Pattern for testing error handling across layers."""
    from unittest.mock import MagicMock, create_autospec

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from faultmaven.models.interfaces import IKnowledgeIngester, ITracer
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    def knowledge_service(vector_store):
        sanitizer = create_autospec(DataSanitizer, instance=True)
        sanitizer.asanitize.side_effect = lambda data: data
        return KnowledgeService(
            knowledge_ingester=create_autospec(IKnowledgeIngester, instance=True),
            sanitizer=sanitizer,
            tracer=create_autospec(ITracer, instance=True),
            vector_store=vector_store,
            db_session_factory=MagicMock(),
        )

    # Create mock that simulates external service failure
    vector_store = create_autospec(KnowledgeVectorStore, instance=True)
    vector_store.search.side_effect = ConnectionError("Vector store unavailable")

    # Test 1: Error propagation through the service layer
    with pytest.raises(ConnectionError) as exc_info:
        await knowledge_service(vector_store).search_knowledge("test")

    assert "Vector store unavailable" in str(exc_info.value)

    # Test 2: Invalid input is refused before any dependency is reached
    with pytest.raises(ValueError, match="Query cannot be empty"):
        await knowledge_service(vector_store).search_knowledge("   ")
    assert vector_store.search.await_count == 1  # only Test 1's call

    # Test 3: Graceful degradation: no vector store, no results
    assert await knowledge_service(vector_store=None).search_knowledge("test") == []
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

    # Perform operations (a getter never composes the container, #2249:
    # this measures reset and lookup, not composition)
    iterations = 100
    for i in range(iterations):
        container.reset()
        agent_service = container.get_agent_service()
        knowledge_service = container.get_knowledge_service()

        # Validate nothing is composed behind the caller's back
        assert agent_service is None
        assert knowledge_service is None

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
def test_calibrated_performance():
    """A wall-clock threshold goes through the shared helper, never a literal."""
    from tests.wallclock import assert_latency_within

    from .budgets import SOME_OPERATION

    start = time.perf_counter()
    do_the_work()
    elapsed = time.perf_counter() - start

    assert_latency_within(elapsed, SOME_OPERATION, "Some operation")
```

**Key Principles**:
- Measure actual performance metrics
- Never compare a duration against a literal. `tests/benchmarks/` and
  `tests/performance/` both route every threshold through
  `assert_latency_within` / `assert_throughput_at_least`, so the machine
  calibration applies and the per-PR / nightly split holds. An AST scan in
  `tests/unit/ci/test_benchmark_calibration.py` fails the build on a
  hand-rolled spelling, and a second check fails on a test that takes a
  clock reading and never reaches a helper.
- Anchor a threshold 2-3x above measured cost and record the runs it came
  from, in that directory's `budgets.py` (#1556/#1557)
- Do NOT gate a performance test behind an opt-in environment variable: a
  skipped test is a budget nobody applies. `RUN_PERFORMANCE_TESTS` was
  removed in #1557 for that reason
- Monitor memory usage and cleanup. Megabytes and object counts are NOT
  calibrated — machine throughput does not move them

## Advanced Testing Patterns

### 7. Parameterized Testing Pattern

**Purpose**: Test multiple scenarios with different parameters

```python
from faultmaven.config.settings import LLMProvider

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

        # Test provider-specific configuration (keys are SecretStr)
        if provider == "openai":
            assert settings.llm.openai_api_key.get_secret_value() == 'test-api-key-value'
        elif provider == "fireworks":
            assert settings.llm.fireworks_api_key.get_secret_value() == 'test-api-key-value'
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
    import asyncio
    from unittest.mock import MagicMock, create_autospec

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from faultmaven.models.interfaces import IKnowledgeIngester, ITracer
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    # Setup async mocks: autospec makes each async method an AsyncMock
    vector_store = create_autospec(KnowledgeVectorStore, instance=True)
    vector_store.search.return_value = [
        {"id": "kb_a_chunk_0", "content": "Mock search result 1", "score": 0.95},
        {"id": "kb_b_chunk_0", "content": "Mock search result 2", "score": 0.87},
    ]
    sanitizer = create_autospec(DataSanitizer, instance=True)
    sanitizer.asanitize.side_effect = lambda data: data

    knowledge_service = KnowledgeService(
        knowledge_ingester=create_autospec(IKnowledgeIngester, instance=True),
        sanitizer=sanitizer,
        tracer=create_autospec(ITracer, instance=True),
        vector_store=vector_store,
        db_session_factory=MagicMock(),
    )

    # Concurrent async operations
    first_task = knowledge_service.search_knowledge("connection timeout")
    second_task = knowledge_service.search_knowledge("pool exhausted")

    # Wait for both operations
    first, second = await asyncio.gather(first_task, second_task)

    # Validate async results
    assert [r.score for r in first] == [0.95, 0.87]
    assert len(second) == 2

    # Validate async mock calls
    assert vector_store.search.await_count == 2
    assert sanitizer.asanitize.await_count == 2
```

**Key Principles**:
- Use `@pytest.mark.asyncio` for async tests
- Use `AsyncMock` for async interface methods (`create_autospec` makes them)
- Test concurrent operations with `asyncio.gather()`
- Validate both results and mock interactions

### 9. Configuration Testing Pattern

**Purpose**: Test complex configuration scenarios

```python
def test_configuration_pattern(clean_env):
    """Pattern for testing complex configuration scenarios."""
    from faultmaven.config.settings import (
        Environment,
        LogLevel,
        get_settings,
        reset_settings,
    )
    from faultmaven.models.exceptions import ConfigurationError

    # Test 1: Development configuration
    dev_config = {
        'ENVIRONMENT': 'development',
        'DEBUG': 'true',
        'LOG_LEVEL': 'DEBUG',
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'sk-dev-key'
    }

    with patch.dict(os.environ, dev_config):
        reset_settings()  # each scenario builds its own settings
        settings = get_settings()
        assert settings.server.environment == Environment.DEVELOPMENT
        assert settings.server.debug is True
        assert settings.logging.level == LogLevel.DEBUG

    # Test 2: Production configuration
    prod_config = {
        'ENVIRONMENT': 'production',
        'DEBUG': 'false',
        'LOG_LEVEL': 'INFO',
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'fw-prod-key',
        'REDIS_HOST': 'redis.test',
        'REDIS_PORT': '6380',
        'CHROMADB_URL': 'http://chromadb.test:8000'
    }

    with patch.dict(os.environ, prod_config):
        reset_settings()
        settings = get_settings()
        assert settings.server.environment == Environment.PRODUCTION
        assert settings.server.debug is False
        assert settings.logging.level == LogLevel.INFO
        assert settings.database.redis_host == 'redis.test'

    # Test 3: Invalid configuration handling
    invalid_config = {
        'CHAT_PROVIDER': 'invalid_provider',
        'REDIS_PORT': 'not_a_number'
    }

    with patch.dict(os.environ, invalid_config):
        reset_settings()
        # get_settings() wraps the pydantic ValidationError
        with pytest.raises(ConfigurationError) as exc_info:
            get_settings()

        assert "invalid_provider" in str(exc_info.value)
```

**Key Principles**:
- Test realistic configuration scenarios
- Test both valid and invalid configurations
- Use separate environment contexts for each test
- Validate all aspects of configuration processing

## Testing Utilities and Fixtures

### Common Test Fixtures

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

`clean_env` (pattern 3) is a test module's own fixture: `tests/conftest.py`
defines none.

### Test Helper Functions

```python
# tests/case_builders.py and tests/utils.py: plain keyword-override builders

from faultmaven.modules.case.contracts import CaseState
from tests.case_builders import inquiry_case, investigating_case
from tests.utils import generate_case_id, generate_user_id

def test_with_built_cases():
    """Build the case a test needs, overriding only the fields that matter."""
    case = investigating_case(
        case_id=generate_case_id(),
        user_id=generate_user_id(),
        statement="Database connections time out",
    )
    assert case.state == CaseState.INVESTIGATING
    assert case.inquiry.problem_statement_confirmed is True

    fresh = inquiry_case(statement="Pods crash on start")
    assert fresh.state == CaseState.INQUIRY
    assert fresh.inquiry.problem_statement_confirmed is False
```

## Best Practices Summary

### Testing Pattern Checklist

- ✅ **Container Reset**: Always call `container.reset()` before test logic
- ✅ **Interface Mocking**: Use `create_autospec(Interface, instance=True)` for dependency mocking
- ✅ **Environment Isolation**: Use clean environment fixtures
- ✅ **Async Testing**: Use `AsyncMock` for async interface methods
- ✅ **Error Testing**: Test both success and failure scenarios
- ✅ **Performance Testing**: Monitor container and service performance
- ✅ **Integration Testing**: Test cross-layer workflows
- ✅ **Resource Management**: Ensure proper cleanup and isolation

### Common Anti-Patterns to Avoid

❌ **Mocks on the Container**: Don't assign mocks onto container attributes
✅ **Constructor Injection**: Pass mocks to the unit under test; use `container.get_*_service()` after `await container.initialize()`

❌ **Unspecced Mocks**: Don't use a bare `Mock()`; it answers any call
✅ **Interface Mocking**: Spec on the interface (`ITracer`), or on the class the container passes where no interface declares the call

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
