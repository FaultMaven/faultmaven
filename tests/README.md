# FaultMaven Test Suite

This directory contains the comprehensive test suite for FaultMaven after a major test architecture overhaul, featuring **1425+ tests** across unit, integration, API, performance, and architecture categories, organized by Clean Architecture layers with **dependency injection** and **interface-based design**.

## Test Architecture Overview ✅

Major test architecture overhaul completed with:

- **Clean Architecture Testing**: New patterns for DI container usage and interface mocking
- **1425+ Tests**: Comprehensive coverage across architectural layers
- **4 New Comprehensive Test Files**: Settings system, LLM registry, container integration, architecture workflows
- **Container-Based Testing**: All tests use dependency injection container patterns
- **Interface Compliance**: All dependencies use interface contracts with proper mocking
- **Performance Focus**: <0.5% logging overhead with comprehensive validation
- **Application Issues Documented**: Separate remediation tracking for identified issues

## Test Structure

The test directory is organized by architectural layer following clean architecture principles:

```
tests/
├── __init__.py
├── conftest.py                 # Shared fixtures and configuration
├── README.md                   # This file (updated after architecture overhaul)
├── api/                        # API Layer Tests (FastAPI endpoints)
│   ├── conftest.py            # API-specific fixtures
│   ├── middleware/            # Middleware testing
│   │   └── test_context_management.py
│   ├── test_agent_endpoints.py     # Agent query processing endpoints
│   ├── test_data_endpoints.py      # Data ingestion endpoints  
│   ├── test_end_to_end_workflows.py # Complete workflows
│   ├── test_knowledge_endpoints.py # Knowledge base management
│   ├── test_performance_validation.py # API performance
│   ├── test_query_processing.py    # Query processing workflows
│   ├── test_session_endpoints.py   # Session management endpoints
│   ├── test_404_fixes.py           # API error handling fixes
│   ├── test_compliance_validation.py # Schema compliance validation
│   └── test_contract_minimal.py    # API contract testing
├── core/                       # Core Domain Layer Tests
│   ├── test_classifier.py     # Data classification logic
│   ├── test_core_agent.py     # Core agent functionality
│   ├── test_core_agent_errors.py # Agent error handling
│   ├── test_doctrine.py       # 5-phase SRE doctrine
│   ├── test_ingestion.py      # Knowledge base ingestion
│   ├── test_log_processor.py  # Log processing logic
│   └── tools/                 # Agent Tools
│       ├── test_knowledge_base.py  # RAG knowledge base tool
│       └── test_web_search.py      # Web search capabilities
├── infrastructure/             # Infrastructure Layer Tests
│   ├── test_chromadb_store.py # Vector store integration
│   ├── test_external_clients.py # External service clients
│   ├── test_infrastructure_utils.py # Infrastructure utilities
│   ├── test_llm_providers.py  # Multi-LLM provider testing
│   ├── test_observability_integration.py # Tracing integration
│   ├── test_persistence_integration.py # Database integration
│   ├── test_phase2_monitoring_fixed.py # Fixed system monitoring
│   ├── test_redaction.py      # PII redaction and sanitization
│   ├── test_redaction_errors.py # Sanitization error handling
│   ├── test_redis_case_store.py # Case persistence
│   ├── test_redis_session_store.py # Session persistence
│   ├── test_router.py         # LLM routing logic
│   ├── test_security_processing.py # Security processing
│   └── test_llm_registry_comprehensive.py # NEW: LLM registry management (37+ tests)
├── services/                   # Service Layer Tests
│   ├── test_agent.py  # Agent service orchestration
│   ├── test_data.py   # Data service operations
│   ├── test_knowledge.py # Knowledge service operations
│   ├── test_service_integration.py # Cross-service integration
│   ├── test_service_performance.py # Service performance testing
│   └── test_session.py # Session management
├── unit/                       # Unit Tests for Architecture Components
│   ├── test_container_foundation.py # DI container foundation (thread safety)
│   ├── test_dependency_injection.py # DI patterns validation
│   ├── test_feature_flags.py  # Feature flag management
│   ├── test_interface_compliance_new.py # Interface compliance validation
│   ├── test_tools_registry.py # Tools registry testing
│   ├── test_api_models_v3.py  # Fixed API model validation
│   ├── test_response_type_logic.py # Response type handling
│   ├── test_settings_system_comprehensive.py # NEW: Complete settings system (37+ tests)
│   └── test_container_integration_comprehensive.py # NEW: DI container integration (38+ tests)
├── integration/                # Cross-Layer Integration Tests
│   ├── __init__.py
│   ├── conftest.py            # Integration test fixtures
│   ├── mock_servers.py        # Mock API servers for external services
│   ├── pytest.ini            # Integration test configuration
│   ├── README.md              # Integration test documentation
│   ├── test_kb_ingestion_and_indexing.py # Knowledge base integration
│   ├── test_readiness_and_redis.py # System readiness testing
│   ├── test_api_compliance_integration.py # API compliance integration
│   ├── test_case_persistence_end_to_end.py # Case persistence workflows
│   ├── test_system_performance_integration.py # Performance integration
│   └── test_new_architecture_workflows.py # NEW: Architecture workflow testing (18+ tests)
├── performance/                # Performance Tests (Conditional Execution)
│   ├── test_context_overhead.py # Context creation performance
│   └── test_logging_overhead.py # Logging performance validation
├── test_architecture.py       # Architecture validation and compliance
├── test_doubles.py            # Unified test utilities and fixtures
├── test_main.py               # Application lifecycle and startup tests
├── test_observability_core.py # Core observability and tracing tests
└── test_session_service.py # Session service unit tests (replaces session_management)
```

## Test Categories

### Unit Tests
- **Security Tests** (`@pytest.mark.security`): Data sanitization, PII detection
- **Data Processing Tests** (`@pytest.mark.data_processing`): Classification, log processing
- **Agent Tests** (`@pytest.mark.agent`): Knowledge base tools, agent logic
- **API Tests** (`@pytest.mark.api`): FastAPI endpoints, request/response handling
- **LLM Tests** (`@pytest.mark.llm`): LLM router, provider management
- **Session Tests** (`@pytest.mark.session`): Session management, lifecycle

### Integration Tests
Located in `tests/integration/`, these tests validate end-to-end workflows with mock infrastructure. Recent cleanup removed over-engineered integration tests in favor of focused service-layer tests with proper mocking.

### Mock API Infrastructure
The integration tests include sophisticated mock API servers that simulate:

- **LLM Providers**: OpenAI-compatible APIs (Fireworks, OpenRouter)
- **Ollama API**: Local LLM provider simulation  
- **Web Search APIs**: Google Custom Search and Tavily APIs
- **Intelligent Responses**: Context-aware mock responses for realistic testing

## Running Tests

### Prerequisites

Install test dependencies:
```bash
pip install -r requirements-test.txt
```

**Environment Configuration for Testing**:
```bash
# Skip external service checks for unit testing
export SKIP_SERVICE_CHECKS=true

# Enable debug logging for test debugging
export LOG_LEVEL=DEBUG

# Set test-specific Redis configuration
export REDIS_HOST=192.168.0.111
export REDIS_PORT=30379
```

For integration tests, ensure Docker services are running:
```bash
docker-compose up -d
```

### Basic Test Execution

**Full Test Suite (1425+ tests)**:
```bash
# All tests with external service bypass
SKIP_SERVICE_CHECKS=true python -m pytest tests/ --cov=faultmaven

# Run with coverage report
pytest --cov=faultmaven --cov-report=html

# Run all tests using advanced test runner
python run_tests.py --all --coverage --html
```

**New Comprehensive Architecture Tests**:
```bash
# New settings system tests (37+ tests)
python -m pytest tests/unit/test_settings_system_comprehensive.py -v

# New LLM registry tests (37+ tests) 
python -m pytest tests/infrastructure/test_llm_registry_comprehensive.py -v

# New container integration tests (38+ tests)
python -m pytest tests/unit/test_container_integration_comprehensive.py -v

# New architecture workflow tests (18+ tests)
python -m pytest tests/integration/test_new_architecture_workflows.py -v
```

### Test Categories

**Container-Based Architecture Tests**:
```bash
# Unit tests (DI container, interfaces, settings)
SKIP_SERVICE_CHECKS=true pytest tests/unit/ -v

# Service layer tests with container injection
pytest tests/services/ -v

# Infrastructure tests with interface mocking
pytest tests/infrastructure/ -v

# New architecture integration workflows
pytest tests/integration/test_new_architecture_workflows.py -v
```

**Test Execution by Layer**:
```bash
# API tests (FastAPI endpoints and middleware)
pytest tests/api/ -v

# Core domain tests (agent, processing, tools)
pytest tests/core/ -v

# Architecture validation tests
pytest tests/test_architecture.py -v

# Application integration tests
pytest tests/test_main.py -v
```

**Advanced Test Categories**:
```bash
# Performance tests (conditional execution)
RUN_PERFORMANCE_TESTS=true pytest tests/performance/ -v

# Security tests (PII redaction and sanitization)
pytest -m security -v

# Integration tests (cross-layer workflows)
pytest tests/integration/ -v

# Observability tests (tracing and logging)
pytest tests/test_observability_core.py tests/infrastructure/test_observability_integration.py -v
```

### Integration Tests

Run integration tests with mock infrastructure:
```bash
# Mock API infrastructure tests
pytest tests/integration/ -v
```

**Note**: Mock API tests should be run individually from the `tests/integration/` directory to avoid port conflicts.

### Advanced Options

Parallel execution:
```bash
pytest -n auto
```

Verbose output:
```bash
pytest -v
```

Generate HTML coverage report:
```bash
pytest --cov=faultmaven --cov-report=html:htmlcov
```

### Using the Advanced Test Runner Script

The `run_tests.py` script provides comprehensive test execution with architecture validation:

```bash
# Complete test suite with linting, type checking, security analysis
python run_tests.py --all --coverage --html

# Unit tests with container and interface validation
python run_tests.py --unit --coverage

# Integration tests with architecture workflows
python run_tests.py --integration

# Security tests with PII protection validation
python run_tests.py --security

# Performance tests (conditional execution)
RUN_PERFORMANCE_TESTS=true python run_tests.py --performance

# Code quality checks only
python run_tests.py --lint --type-check

# API contract and compliance testing
python run_tests.py --api
```

**Container-Based Test Execution**:
```bash
# Test with DI container reset and clean state
SKIP_SERVICE_CHECKS=true python run_tests.py --unit

# Test with mock service patterns
python run_tests.py --integration --mock-services

# Test interface compliance across all layers
python run_tests.py --interface-compliance
```

## Test Suite Architecture Benefits

The reorganized test suite provides several key benefits:

### **Consolidated Infrastructure**
- **Unified Test Utilities**: All test doubles and fixtures in single module
- **Minimal Mocking Strategy**: External boundaries only, real business logic testing
- **Interface Compliance**: All dependencies use interface contracts
- **Performance Focus**: <0.5% logging overhead with comprehensive validation

### **Quality Improvements**
- **Architectural Compliance**: Clean layer separation with dependency injection
- **Realistic Testing**: Business logic validation rather than mock verification
- **Error Handling**: Comprehensive error scenario testing and graceful degradation
- **Cross-Layer Integration**: End-to-end workflow validation with proper coordination

## Mock Infrastructure

The test suite includes mock infrastructure for external dependencies:

### Test Doubles (`test_doubles.py`)
- **LLM Provider Simulation**: Contextual responses for different query types
- **External API Simulation**: Realistic external service behavior with error handling
- **Storage Backends**: In-memory storage with async patterns and cleanup
- **Security Services**: PII redaction simulation with structured data handling

### Mock Server Infrastructure
Available in `tests/integration/mock_servers.py` for external API simulation:
- **LLM Provider APIs**: OpenAI-compatible and Ollama endpoints
- **Web Search APIs**: Google Custom Search and Tavily API simulation
- **Intelligent Responses**: Context-aware mock responses based on query content

## **Enhanced Test Design Principles - Post-Architecture Overhaul**

### **Container-Based Testing Strategy**
- **Dependency Injection**: All tests use DI container patterns with proper service resolution
- **Interface-Based Mocking**: Enhanced mocks implement proper interfaces (`ILLMProvider`, `ISanitizer`, `ITracer`)
- **Clean State Management**: Container reset patterns ensure test isolation
- **Service Lifecycle**: Tests validate complete service initialization and cleanup
- **Real Business Logic**: Minimal mocking with focus on external boundary testing

### **New Architecture Testing Patterns**
- **Settings System Testing**: Complete environment variable processing and validation (37+ tests)
- **LLM Registry Testing**: Centralized provider management with fallback chains (37+ tests)
- **Container Integration**: Full dependency injection lifecycle and interface resolution (38+ tests)
- **Workflow Integration**: End-to-end architecture validation with cross-layer communication (18+ tests)

### **Clean Architecture Testing Compliance**
- **Layer Isolation**: Tests strictly organized by clean architecture layers with proper separation
- **Container Resolution**: All services resolved through DI container with interface contracts
- **Mock Service Patterns**: Test-specific mock implementations for external dependencies
- **Interface Validation**: Comprehensive testing that all implementations meet interface contracts
- **Cross-Layer Communication**: Integration tests validate proper layer interaction patterns

### **Performance & Reliability (1425+ Tests)**
- **Conditional Execution**: Performance tests run only when `RUN_PERFORMANCE_TESTS=true`
- **Container Performance**: <0.5% overhead from DI container operations in tests
- **Async Patterns**: Proper async/await usage with container-injected services
- **Resource Management**: Efficient container cleanup and service isolation between tests
- **Test Execution Speed**: Individual tests complete in <500ms with proper mocking

### **Test Data Standards & New Test Coverage**
- **Comprehensive Scenarios**: Test data covers all new architectural components and workflows
- **Environment Isolation**: Clean environment setup with proper variable management
- **Production-Like Configuration**: Settings tests validate real deployment scenarios
- **Provider Integration**: LLM registry tests cover all 7 supported providers
- **Container States**: Tests validate all container lifecycle states and error conditions
- **Interface Compliance**: All test mocks implement proper interface contracts

## Test Examples - New Architecture Patterns

### Container-Based Unit Test Example
```python
from faultmaven.container import container

def test_settings_system_with_container():
    """Test settings system integration with DI container."""
    # Reset container state for clean testing
    container.reset()
    
    # Test with environment variables
    os.environ['CHAT_PROVIDER'] = 'openai'
    os.environ['OPENAI_API_KEY'] = 'sk-test'
    
    settings = get_settings()
    assert settings.llm.provider == LLMProvider.OPENAI
    assert settings.llm.openai_api_key == 'sk-test'
```

### Interface-Based Service Test Example
```python
@pytest.mark.asyncio
async def test_agent_service_with_mock_interfaces():
    """Test agent service with proper interface mocking."""
    # Mock interfaces rather than implementations
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response.return_value = "Test response"
    
    mock_sanitizer = Mock(spec=ISanitizer)
    mock_sanitizer.sanitize.return_value = "sanitized query"
    
    # Inject mocks through container
    container.reset()
    container._llm_provider = mock_llm
    container._sanitizer = mock_sanitizer
    
    agent_service = container.get_agent_service()
    result = await agent_service.process_query("test query", "session-1")
    
    assert "Test response" in result.response
    mock_sanitizer.sanitize.assert_called_once()
```

### Architecture Workflow Integration Test Example
```python
@pytest.mark.asyncio
async def test_end_to_end_troubleshooting_workflow():
    """Test complete troubleshooting workflow through all layers."""
    # Test Settings → Container → Services → Core flow
    container.reset()
    
    # Initialize with test configuration
    test_settings = {
        'CHAT_PROVIDER': 'fireworks',
        'FIREWORKS_API_KEY': 'test-key',
        'REDIS_HOST': 'localhost'
    }
    
    with patch.dict(os.environ, test_settings):
        # Validate settings layer
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.FIREWORKS
        
        # Validate container initialization
        agent_service = container.get_agent_service()
        assert agent_service is not None
        
        # Validate end-to-end workflow
        result = await agent_service.process_query(
            "Server returning 500 errors", 
            "test-session"
        )
        
        assert result.session_id == "test-session"
        assert len(result.response) > 100
```

## Coverage Requirements - Post-Architecture Overhaul

**Current Test Metrics**:
- **Total Tests**: 1425+ tests across all architectural layers
- **Test Files**: 86 test files organized by Clean Architecture principles
- **New Comprehensive Tests**: 4 major test files covering new architecture components
- **Success Rate**: All core tests passing with proper container isolation

**Coverage Targets**:
- **Minimum Coverage**: 75% (current architectural baseline)
- **Critical Paths**: 95% (DI container, interfaces, LLM registry)
- **Error Handling**: 100% (graceful degradation and fallback scenarios)
- **New Components**: 100% (settings system, container integration, architecture workflows)

## Continuous Integration

Tests are automatically run on:
- Pull requests
- Main branch commits
- Release tags

CI checks include:
- Unit tests
- Integration tests
- Code coverage
- Linting
- Type checking
- Security scanning

## Debugging Tests

### Running Single Tests
```bash
# Run specific test function
pytest tests/security/test_redaction.py::TestDataSanitizer::test_pii_redaction

# Run with debug output
pytest -s -v tests/security/test_redaction.py
```

### Mock API Debugging
```bash
# Run mock API tests from correct directory
cd tests/integration
pytest test_llm_failover.py::test_llm_router_mock_integration -v -s

# Check mock server logs
pytest test_llm_failover.py -v -s --log-cli-level=DEBUG
```

### Test Isolation
```bash
# Run tests in isolation
pytest --dist=no

# Run with maximum verbosity
pytest -vvv
```

### Coverage Analysis
```bash
# Generate detailed coverage report
pytest --cov=faultmaven --cov-report=term-missing

# View HTML coverage report
open htmlcov/index.html
```

## **Optimization History & Maintenance Guidelines**

### **Recent Consolidation (2024)**
The test suite underwent comprehensive optimization and cleanup with the following improvements:

#### **Key Optimizations Applied:**
1. **Mock Sophistication**: Enhanced `MockLogProcessor` from 310+ lines to 70 lines while adding real business logic
2. **Skip Rate Reduction**: Reduced infrastructure test skips from 95% to <5% through better dependency detection
3. **Performance Conditioning**: Made performance tests conditional to prevent CI flakiness
4. **Service Enhancement**: Added missing session service methods and analytics capabilities
5. **Architecture Compliance**: Ensured all tests follow FastAPI and clean architecture patterns
6. **Test Suite Cleanup**: Removed 38 over-engineered tests across 5 files that were testing implementation details rather than business value

#### **Files Modified During Consolidation:**
- **Enhanced**: `tests/services/test_data.py` - Sophisticated mock with anomaly detection
- **Fixed**: `tests/infrastructure/test_targeted_tracing_integration.py` - Function signature alignment
- **Optimized**: `tests/infrastructure/test_opik_initialization_fix.py` - Better fallback mocking
- **Enhanced**: `tests/services/test_session.py` - Added comprehensive analytics
- **Conditioned**: `tests/performance/test_*.py` - Environment-controlled execution
- **Updated**: `faultmaven/services/session.py` - Added missing business methods
- **Migrated**: `faultmaven/models_original.py` → `faultmaven/models/legacy.py` - Model refactoring
- **Removed**: `tests/infrastructure/test_logging_content_verification.py` - Over-engineered logging content tests
- **Removed**: `tests/infrastructure/test_logging_infrastructure_integration.py` - Complex infrastructure logging tests
- **Removed**: `tests/infrastructure/test_logging_request_lifecycle.py` - Request lifecycle logging tests
- **Removed**: `tests/integration/test_logging_cross_layer_coordination.py` - Cross-layer logging coordination tests
- **Removed**: `tests/integration/test_session_lifecycle_enhanced.py` - Outdated session lifecycle tests
- **Removed**: `tests/services/test_logging_service_integration.py` - Service layer logging tests
- **Fixed**: `faultmaven/infrastructure/logging/coordinator.py` - Error storage format for test compatibility
- **Fixed**: `faultmaven/infrastructure/monitoring/` - Metrics aggregation and async context handling
- **Fixed**: `faultmaven/api/v1/routes/` - Removed migration terminology from OpenAPI schema

### **Maintenance Procedures**

#### **Mock Synchronization**
- **Quarterly Review**: Validate that mock behaviors still reflect real implementations
- **Interface Updates**: When service interfaces change, update corresponding test mocks
- **Business Logic Evolution**: Keep `MockLogProcessor` and similar mocks aligned with actual analysis capabilities

#### **Performance Baseline Updates**  
- **Environment Awareness**: Update performance thresholds when infrastructure changes
- **Conditional Execution**: Use `RUN_PERFORMANCE_TESTS=true` only in performance-focused CI runs
- **Threshold Monitoring**: Review and adjust performance expectations quarterly

#### **Dependency Management**
- **External Service Mocking**: Maintain fallback mocks for all external dependencies (Opik, Presidio, etc.)
- **Interface Compliance**: Ensure all mocks implement proper interfaces for realistic testing
- **Graceful Degradation**: Test suite should provide value even when external services unavailable

## Best Practices

### **Writing Tests (Architecture Overhaul Guidelines)**
1. **Container-First Approach**: Always use DI container for service access and reset for clean state
2. **Interface-Based Mocking**: Mock interfaces (`ILLMProvider`, `ISanitizer`) rather than concrete implementations
3. **Environment Isolation**: Use clean environment fixtures for proper test isolation
4. **Real Business Logic**: Focus on testing actual business outcomes through injected dependencies
5. **Async Patterns**: Follow `pytest-asyncio` patterns with proper container service injection
6. **Settings Integration**: Test configuration scenarios with realistic environment variables
7. **Error Scenario Coverage**: Test all error conditions and graceful degradation paths

### **Test Maintenance - New Architecture Standards**
1. **Container Lifecycle**: Maintain proper container reset and service isolation patterns
2. **Interface Compliance**: Ensure all new mocks implement proper interface contracts
3. **Settings Validation**: Keep environment variable testing aligned with production configuration
4. **Provider Registry**: Maintain LLM provider tests as new providers are added
5. **Architecture Compliance**: Ensure all tests follow Clean Architecture layer boundaries
6. **Performance Monitoring**: Monitor container overhead and test execution performance
7. **Integration Testing**: Maintain cross-layer communication patterns and workflow testing

### Common Patterns - New Architecture Testing

```python
# Container-based testing with clean state
from faultmaven.container import container

def test_with_container_reset():
    container.reset()  # Clean state for isolation
    service = container.get_agent_service()
    assert service is not None

# Environment variable testing with isolation
@pytest.fixture
def clean_env():
    original_env = os.environ.copy()
    # Clear test-related env vars
    yield
    os.environ.clear()
    os.environ.update(original_env)

# Interface-based mocking
def test_with_interface_mocks():
    mock_llm = Mock(spec=ILLMProvider)
    mock_llm.generate_response.return_value = "test"
    
    container.reset()
    container._llm_provider = mock_llm
    
# Settings system testing
def test_settings_configuration():
    with patch.dict(os.environ, {
        'CHAT_PROVIDER': 'openai',
        'OPENAI_API_KEY': 'test-key'
    }):
        settings = get_settings()
        assert settings.llm.provider == LLMProvider.OPENAI

# Parameterized provider testing
@pytest.mark.parametrize("provider,expected", [
    ("openai", LLMProvider.OPENAI),
    ("fireworks", LLMProvider.FIREWORKS),
    ("anthropic", LLMProvider.ANTHROPIC),
])
def test_provider_configuration(provider, expected):
    # Test multiple provider configurations
```

## Troubleshooting

### Common Issues

**Import Errors**: Ensure `PYTHONPATH` includes project root
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

**Async Test Issues**: Use `pytest-asyncio` and `@pytest.mark.asyncio`
```python
@pytest.mark.asyncio
async def test_async_function():
    # async test code
```

**Mock API Port Conflicts**: Run mock API tests individually
```bash
cd tests/integration
pytest test_llm_failover.py::test_llm_router_mock_integration -v
```

**Docker Service Issues**: Ensure services are running for integration tests
```bash
docker-compose up -d
docker-compose ps  # Check service health
```

### Performance Issues

**Slow Tests**: Use parallel execution
```bash
pytest -n auto
```

**Memory Issues**: Clean up resources in fixtures
```python
@pytest.fixture(autouse=True)
def cleanup():
    yield
    # cleanup code
```

## Utilities

### Debug Scripts
- Test utilities available in `tests/utils/` directory

### Test Fixtures
- `tests/conftest.py`: Global test fixtures
- `tests/integration/conftest.py`: Integration test fixtures

## Contributing

When adding new tests:

1. Follow the existing test structure
2. Add appropriate markers
3. Update coverage requirements if needed
4. Document new test patterns
5. Ensure all tests pass before submitting
6. For integration tests, verify Docker services are available

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [pytest-mock](https://pytest-mock.readthedocs.io/)
- [Coverage.py](https://coverage.readthedocs.io/)
- [Docker Compose](https://docs.docker.com/compose/)
- [FaultMaven Integration Tests](./integration/README.md) 