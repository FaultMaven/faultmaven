# FaultMaven Architecture Testing Guide

**Document Type**: Architecture Testing Guide
**Last Updated**: August 2025
**Context**: Post-Architecture Overhaul - Clean Architecture Testing

## Overview

This comprehensive testing guide covers FaultMaven's Clean Architecture testing implementation after the major test architecture overhaul. With **1425+ tests** across architectural layers, our container-based testing strategy ensures reliability through **dependency injection**, **interface-based mocking**, and **comprehensive architecture validation** including the **4 new comprehensive test files** covering settings, LLM registry, container integration, and architecture workflows.

## Clean Architecture Testing Structure

### Test Organization by Architectural Layer

FaultMaven's tests follow the Clean Architecture layering:

```
tests/
├── conftest.py             # Global test configuration and fixtures (reset_container, booted_app_client)
├── case_builders.py        # Plain keyword-override Case builders
├── network_guard.py        # Refuses non-loopback connects and foreign names; admits the job's PostgreSQL/Redis, never a model-server port
├── unit/                   # Unit tests, by layer
│   ├── api/                # API Layer: routes, middleware, composition root
│   ├── modules/            # Feature modules: agent, auth, case, evidence, knowledge, preprocessing, report
│   ├── core/               # Core Domain: investigation, knowledge, preprocessing
│   ├── infrastructure/     # Infrastructure adapters
│   ├── services/           # Service base class
│   ├── architecture/       # Module boundaries and contract conformance
│   ├── test_container_foundation.py                # DI container foundation
│   ├── test_dependency_injection.py                # DI patterns validation
│   └── test_container_integration_comprehensive.py # Container integration (18 tests)
├── infrastructure/         # Infrastructure Layer Testing (External integrations)
│   ├── test_llm_providers.py       # Multi-LLM provider testing
│   ├── test_redaction.py           # PII redaction and sanitization
│   ├── test_redis_session_store.py # Session persistence
│   ├── test_chromadb_store.py      # Vector store integration
│   ├── test_router.py              # LLM provider routing
│   └── test_llm_registry_comprehensive.py # LLM registry (52 tests)
├── integration/            # Cross-Layer Integration Testing
│   ├── conftest.py                 # Integration test fixtures
│   ├── mock_servers.py             # get_free_port() for loopback servers
│   ├── test_kb_ingestion_and_indexing.py # Knowledge base integration
│   └── test_readiness_and_redis.py # System readiness testing
├── benchmarks/             # Benchmarks, against benchmarks/budgets.py
├── performance/            # Wall-clock overhead, against performance/budgets.py
│   ├── test_logging_overhead.py    # Logging performance validation
│   └── test_context_overhead.py    # Context creation performance
└── load/                   # Locust load tests
```

### Comprehensive Test Files

**1. LLM Registry Testing** (`tests/infrastructure/test_llm_registry_comprehensive.py`)
- 52 tests across 8 test classes covering centralized provider management
- Multi-provider fallback chains and health monitoring
- API key security and provider registration
- Registry state management and concurrency safety

**2. Container Integration Testing** (`tests/unit/test_container_integration_comprehensive.py`)
- 18 tests across 8 test classes covering complete DI container system
- Service lifecycle management and dependency resolution
- Interface compliance and injection patterns
- Container health monitoring and diagnostics

## Clean Architecture Test Categories

### Container-Based Test Execution

Tests are organized around Clean Architecture principles with container-based dependency injection:

```ini
# Test marker definitions (pytest.ini); --strict-markers refuses any other
[pytest]
markers =
    unit: Unit tests (fast, isolated)
    integration: Integration tests (cross-layer)
    slow: Slow running tests (the required Test Standalone/Test Cloud jobs run them; scripts/tests.py --ci skips them)
    cloud: Cloud tests requiring Redis, PostgreSQL
    postgres: Tests that require a real PostgreSQL (skipped unless DATABASE_URL is postgresql)
    security: Security-related tests
    benchmark: Performance benchmarks (baseline tracking)
    performance: Wall-clock tests: tests/performance and the defensive growth checks (excluded from the required jobs; run in benchmarks.yml)
    asyncio: Async tests
    api: API endpoint tests
    llm: LLM-related tests
    agent: Agent-related tests
    session: Session management tests
    knowledge_base: Knowledge base tests
    architecture: Architecture validation tests
    phase3: Phase 3 specific validation tests
    real_llm: Real-LLM integration tests (hits live provider API; opt-in via `pytest -m real_llm` and provider API key env var; excluded from default test runs)
```

### Test Execution Patterns

```bash
# Container-based architecture testing
SKIP_SERVICE_CHECKS=true pytest tests/unit/ -v        # Unit tests with container isolation
pytest tests/unit/services/ -v                       # Service tests with interface injection
pytest tests/integration/ -v                         # Cross-layer workflow validation

# Comprehensive architecture test suites
pytest tests/infrastructure/test_llm_registry_comprehensive.py -v    # LLM registry
pytest tests/unit/test_container_integration_comprehensive.py -v     # Container integration

# Performance overhead (calibrated budgets; no opt-in flag since #1557)
pytest tests/performance/ -v
```

### Running Enhanced Tests by Category

```bash
# All tests (the default run excludes real_llm)
pytest

# Unit tests only (fastest)
pytest -m unit

# Integration tests (in-process; no Docker services)
pytest -m integration

# Security and privacy tests
pytest -m security

# API endpoint tests
pytest -m api

# Enhanced AI agent functionality tests
pytest -m agent

# Enhanced LLM provider and routing tests
pytest -m llm

# Enhanced multi-session management tests (client-based resumption)
pytest -m session

# Wall-clock tests (tests/performance and the growth checks)
pytest -m performance
```

## Architecture Testing Best Practices

### Container State Management

**Always Reset Container**: Use `container.reset()` before each test to ensure clean state:
```python
def test_example():
    from faultmaven.container import container
    container.reset()  # Clean state
    # ... test code
```

**Service Resolution Through Container**: Get a composed service through container methods, after composing the container; a unit test builds its unit directly, with mocked dependencies ([patterns.md](patterns.md), pattern 2):
```python
# Correct: Compose the container, then use it for service resolution
await reset_container.initialize()  # a getter never composes it (#2249)
agent_service = reset_container.get_agent_service()
knowledge_service = reset_container.get_knowledge_service()

# Incorrect: Assigning mocks onto the container
# reset_container.llm_provider = mock_llm  # Don't do this: the composed
#                                          # services already hold the real one
```

### Interface Compliance Testing

**Mock Interfaces, Not Implementations**: Mock the interface contracts:
```python
from unittest.mock import create_autospec

from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.models.interfaces import ILLMProvider

# Correct: Mock interface (create_autospec checks each call's signature, and
# makes the async route an AsyncMock)
mock_llm = create_autospec(ILLMProvider, instance=True)
mock_llm.route.return_value = LLMResponse(
    content="response", confidence=0.9, provider="test",
    model="test-model", tokens_used=0, response_time_ms=0,
)

# Incorrect: Mock concrete implementation
# mock_llm = Mock(spec=OpenAIProvider)  # Don't do this
```

Spec a concrete class only where no interface declares the call the code makes, and treat it as a known gap. The current cases: `KnowledgeService`'s sanitizer and vector store (#2284; [patterns.md](patterns.md), pattern 2), and the milestone engine's tool registry, typed `Any`, with no interface for `AgentToolRegistry` ([standards.md](standards.md)).

**Validate Interface Methods**: Ensure all required methods are implemented. `tests/unit/architecture/test_contract_signature_conformance.py` checks this, signatures included, for every module contract registered in it:
```python
def test_service_implements_interface():
    import inspect

    from faultmaven.modules.knowledge.contracts import IKnowledgeService
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    # Validate interface methods exist
    for name, member in vars(IKnowledgeService).items():
        if inspect.isfunction(member) and not name.startswith('_'):
            assert callable(getattr(KnowledgeService, name, None)), name
```

### Environment Management

**Clean Environment Isolation**: Use fixtures for environment management:
```python
@pytest.fixture
def clean_env():
    original_env = os.environ.copy()
    # Clear test-related variables
    yield
    os.environ.clear()
    os.environ.update(original_env)
```

**Configuration Testing**: Test with realistic environment scenarios:
```python
def test_production_configuration(clean_env):
    from faultmaven.config.settings import Environment, get_settings, reset_settings

    prod_config = {
        'ENVIRONMENT': 'production',
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'prod-key'
    }

    with patch.dict(os.environ, prod_config):
        reset_settings()  # get_settings() caches the instance it built first
        try:
            settings = get_settings()
            assert settings.server.environment == Environment.PRODUCTION
        finally:
            reset_settings()
```

### Performance and Resource Testing

‼ **Never compare a duration against a literal** in `tests/benchmarks/`
or `tests/performance/`. Both route every threshold through
`assert_latency_within` / `assert_throughput_at_least` from
`tests/wallclock`, against a row of that suite's `budgets.py`, so the
machine calibration applies and the per-PR / nightly split holds. Two
checks in `tests/unit/ci/test_benchmark_calibration.py` fail the build
otherwise — an AST scan on the comparison's shape, and a reachability
check that every test taking a clock reading ends up at a helper. See
[performance.md](performance.md).

**Do NOT gate a performance test behind an environment variable.**
`RUN_PERFORMANCE_TESTS` was removed in #1557: it skipped nine tests "to
avoid CI flakiness", which is what the calibration is for, and a skipped
test is a budget nobody applies.

## Testing Documentation Reference

For comprehensive testing patterns and examples, see:
- [tests/README.md](../../../tests/README.md) - Complete test suite documentation
- [architecture.md](architecture.md) - Detailed architecture testing patterns
- [patterns.md](patterns.md) - Modern testing patterns for Clean Architecture
- [tests/integration/README.md](../../../tests/integration/README.md) - Cross-layer integration testing

## Conclusion

FaultMaven's Clean Architecture testing approach with **1425+ tests** ensures comprehensive validation of all architectural components while maintaining proper isolation and realistic testing scenarios. The container-based dependency injection system enables robust testing patterns that validate both individual components and complete system workflows.

Key benefits:
- **Clean State Management**: Proper container reset ensures test isolation
- **Interface Compliance**: All dependencies tested through interface contracts
- **Architecture Validation**: Tests validate Clean Architecture layer boundaries
- **Performance Monitoring**: Timings judged against `budgets.py` rows through the shared helpers
- **Cross-Layer Integration**: End-to-end workflow testing through all layers
