# FaultMaven Architecture Testing Guide

**Document Type**: Architecture Testing Guide
**Last Updated**: August 2025
**Context**: Post-Architecture Overhaul - Clean Architecture Testing

## Overview

This comprehensive testing guide covers FaultMaven's Clean Architecture testing implementation after the major test architecture overhaul. With **1425+ tests** across architectural layers, our container-based testing strategy ensures reliability through **dependency injection**, **interface-based mocking**, and **comprehensive architecture validation** including the **4 new comprehensive test files** covering settings, LLM registry, container integration, and architecture workflows.

## Clean Architecture Testing Structure

### Test Organization by Architectural Layer

FaultMaven's tests strictly follow Clean Architecture layering with container-based dependency injection:

```
tests/
├── api/                    # API Layer Testing (FastAPI with container injection)
│   ├── test_agent_endpoints.py     # Agent query processing endpoints
│   ├── test_data_endpoints.py      # Data upload and processing endpoints
│   ├── test_knowledge_endpoints.py # Knowledge base management endpoints
│   ├── test_session_endpoints.py   # Session lifecycle endpoints
│   └── test_404_fixes.py           # API error handling and fixes
├── services/               # Service Layer Testing (Business logic orchestration)
│   ├── test_agent.py       # Agent service with injected dependencies
│   ├── test_data.py        # Data service operations
│   ├── test_knowledge.py   # Knowledge service operations
│   ├── test_session.py     # Session management
│   └── test_memory.py      # Memory management service
├── core/                   # Core Domain Testing (Business logic)
│   ├── test_classifier.py          # Data classification logic
│   ├── test_core_agent.py          # Core agent functionality
│   ├── test_core_agent_errors.py   # Agent error handling
│   ├── test_doctrine.py            # SRE troubleshooting doctrine
│   ├── test_ingestion.py           # Knowledge ingestion
│   └── test_log_processor.py       # Log analysis logic
├── infrastructure/         # Infrastructure Layer Testing (External integrations)
│   ├── test_llm_providers.py       # Multi-LLM provider testing
│   ├── test_redaction.py           # PII redaction and sanitization
│   ├── test_redis_session_store.py # Session persistence
│   ├── test_chromadb_store.py      # Vector store integration
│   ├── test_router.py              # LLM provider routing
│   └── test_llm_registry_comprehensive.py # NEW: LLM registry (37+ tests)
├── unit/                   # Architecture Component Testing (DI container, interfaces)
│   ├── test_container_foundation.py # DI container foundation
│   ├── test_dependency_injection.py # DI patterns validation
│   └── test_container_integration_comprehensive.py # NEW: Container integration (38+ tests)
├── integration/            # Cross-Layer Integration Testing
│   ├── conftest.py                 # Integration test fixtures
│   ├── mock_servers.py             # get_free_port() for loopback servers
│   ├── test_kb_ingestion_and_indexing.py # Knowledge base integration
│   ├── test_readiness_and_redis.py # System readiness testing
├── performance/            # Performance Testing (Container overhead)
│   ├── test_logging_overhead.py    # Logging performance validation
│   └── test_context_overhead.py    # Context creation performance
└── conftest.py             # Global test configuration and fixtures
```

### Comprehensive Test Files

**1. LLM Registry Testing** (`tests/infrastructure/test_llm_registry_comprehensive.py`)
- 37+ tests across 7 test classes covering centralized provider management
- Multi-provider fallback chains and health monitoring
- API key security and provider registration
- Registry state management and concurrency safety

**2. Container Integration Testing** (`tests/unit/test_container_integration_comprehensive.py`)
- 38+ tests across 9 test classes covering complete DI container system
- Service lifecycle management and dependency resolution
- Interface compliance and injection patterns
- Container health monitoring and diagnostics

## Clean Architecture Test Categories

### Container-Based Test Execution

Tests are organized around Clean Architecture principles with container-based dependency injection:

```python
# Clean Architecture test marker definitions (pytest.ini)
[tool:pytest]
markers =
    unit: Unit tests for architecture components (container, interfaces, settings)
    integration: Cross-layer integration tests with container patterns
    security: Security and PII redaction tests with interface compliance
    api: API endpoint tests with container-injected services
    agent: AI agent tests with dependency injection
    data_processing: Data classification and processing tests
    llm: LLM provider routing and registry tests
    session: Session management and lifecycle tests
    performance: Performance tests including container overhead
```

### Test Execution Patterns

```bash
# Container-based architecture testing
SKIP_SERVICE_CHECKS=true pytest tests/unit/ -v        # Unit tests with container isolation
pytest tests/services/ -v                            # Service tests with interface injection
pytest tests/integration/ -v                         # Cross-layer workflow validation

# Comprehensive architecture test suites
pytest tests/infrastructure/test_llm_registry_comprehensive.py -v    # LLM registry
pytest tests/unit/test_container_integration_comprehensive.py -v     # Container integration

# Performance overhead (calibrated budgets; no opt-in flag since #1557)
pytest tests/performance/ -v
```

### Running Enhanced Tests by Category

```bash
# All tests (including new intelligence tests)
pytest

# Unit tests only (fastest)
pytest -m unit

# Integration tests (requires Docker services)
pytest -m integration

# Security and privacy tests
pytest -m security

# API endpoint tests
pytest -m api

# Enhanced AI agent functionality tests
pytest -m agent

# Enhanced LLM provider and routing tests
pytest -m llm

# Data processing pipeline tests
pytest -m data_processing

# Enhanced multi-session management tests (client-based resumption)
pytest -m session

# Intelligence system tests (new)
pytest -m intelligence

# Memory management tests (new)
pytest -m memory

# Strategic planning tests (new)
pytest -m planning

# Advanced prompting tests (new)
pytest -m prompting

# Performance tests for intelligence services (new)
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
from unittest.mock import AsyncMock, Mock

from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.models.interfaces import ILLMProvider

# Correct: Mock interface
mock_llm = Mock(spec=ILLMProvider)
mock_llm.route = AsyncMock(return_value=LLMResponse(
    content="response", confidence=0.9, provider="test",
    model="test-model", tokens_used=0, response_time_ms=0,
))

# Incorrect: Mock concrete implementation
# mock_llm = Mock(spec=OpenAIProvider)  # Don't do this
```

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
        settings = get_settings()
        assert settings.server.environment == Environment.PRODUCTION
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
- `/tests/README.md` - Complete test suite documentation
- `/tests/ARCHITECTURE_TESTING_GUIDE.md` - Detailed architecture testing patterns
- `/tests/NEW_TEST_PATTERNS.md` - Modern testing patterns for Clean Architecture
- `/tests/integration/README.md` - Cross-layer integration testing

## Conclusion

FaultMaven's Clean Architecture testing approach with **1425+ tests** ensures comprehensive validation of all architectural components while maintaining proper isolation and realistic testing scenarios. The container-based dependency injection system enables robust testing patterns that validate both individual components and complete system workflows.

Key benefits:
- **Clean State Management**: Proper container reset ensures test isolation
- **Interface Compliance**: All dependencies tested through interface contracts
- **Architecture Validation**: Tests validate Clean Architecture layer boundaries
- **Performance Monitoring**: Container overhead and service performance validation
- **Cross-Layer Integration**: End-to-end workflow testing through all layers
