# FaultMaven Integration Tests

This directory contains integration tests that validate cross-layer functionality and complete system workflows after the major test architecture overhaul, featuring **new architecture workflow testing** with **Clean Architecture** and **dependency injection** patterns.

## Overview - Post-Architecture Overhaul

This directory contains integration test infrastructure focused on:

1. **New Architecture Workflows** - End-to-end testing of Clean Architecture patterns with DI container
2. **Container-Based Integration** - Cross-layer validation using dependency injection container
3. **Interface Compliance Testing** - Integration testing with proper interface contracts
4. **Mock API Infrastructure** - External service simulation for testing without dependencies
5. **Cross-Layer Test Utilities** - Shared fixtures and test infrastructure with container support

## Current Test Structure - After Architecture Overhaul

The integration directory contains comprehensive integration test infrastructure with new architecture workflow testing:

### New Architecture Integration Tests

#### `test_architectural_compliance.py` ✅ NEW (2025-10-24)
Comprehensive spec compliance testing with 17 tests validating [case-and-session-concepts.md v2.0](../../docs/architecture/case-and-session-concepts.md):

- **Session Resumption Tests** (3 tests) - client_id-based session resumption validation
- **Multi-Device Support Tests** (2 tests) - Concurrent sessions on multiple devices
- **Case Access Tests** (3 tests) - Case persistence and cross-session access
- **Forbidden Field Validation Tests** (4 tests) - Session updates reject case data
- **Security Tests** (3 tests) - owner_id requirements and ownership enforcement
- **Field Compliance Tests** (2 tests) - Multi-device fields in SessionContext

**Setup:** Tests use Redis configuration from `.env` file. Configure `REDIS_HOST`, `REDIS_PORT`, and `REDIS_PASSWORD` in your `.env`.

**Run tests:**
```bash
# All compliance tests
./run_tests.py --integration

# Specific test
.venv/bin/pytest tests/integration/test_architectural_compliance.py::TestArchitecturalCompliance::test_session_resumption_with_same_client_id -v
```

#### `test_new_architecture_workflows.py` ✅
Comprehensive end-to-end architecture testing with 18+ tests across 5 test classes:

- **TestSettingsContainerServicesFlow** (4 tests) - Settings → Container → Services integration flow
- **TestEndToEndWorkflows** (4 tests) - Complete troubleshooting workflows through all layers
- **TestErrorHandlingAcrossLayers** (4 tests) - Cross-layer error propagation and recovery
- **TestInterfaceComplianceInRealScenarios** (3 tests) - Interface compliance in production scenarios
- **TestCrossLayerCommunicationPatterns** (3 tests) - Layer communication and data flow patterns

**Key Features Tested**:
- Complete troubleshooting workflow from query to response
- Settings system integration with container initialization
- Multi-session case continuity with proper state management
- Error handling and graceful degradation across architectural layers
- Interface compliance validation in real usage scenarios
- Cross-layer communication patterns and data flow

### Additional Integration Tests

#### `test_kb_ingestion_and_indexing.py`
Knowledge base integration testing with vector store operations

#### `test_readiness_and_redis.py`
System readiness and Redis integration validation

#### `test_case_persistence_end_to_end.py`
Complete case persistence workflow testing

#### `test_system_performance_integration.py`
Performance integration testing across architectural layers

#### `test_api_compliance_integration.py`
API compliance and schema validation integration testing

### Core Infrastructure Files

### Infrastructure Files

#### `mock_servers.py`
Mock API server infrastructure for external service simulation:

- **LLM Provider Mocking**: OpenAI-compatible and Ollama API endpoints
- **Web Search Mocking**: Google Custom Search and Tavily API simulation
- **Server Lifecycle Management**: Startup, health monitoring, and graceful shutdown
- **Port Management**: Configurable port assignment and conflict resolution

#### `conftest.py`
Integration test fixtures and configuration:

- **Logging Setup**: Test logging capture and verification utilities
- **Mock Service Configuration**: Shared mock services and test doubles
- **Async Test Support**: Async test infrastructure and cleanup
- **Test Environment**: Integration test environment setup and teardown

## Test Philosophy - New Architecture Integration

### Container-Based Integration Testing
- **Dependency Injection**: All integration tests use DI container patterns for service resolution
- **Clean Architecture Compliance**: Tests validate proper layer separation and communication
- **Interface-Based Testing**: All mocks implement proper interface contracts (`ILLMProvider`, `ISanitizer`, etc.)
- **Real Business Logic**: Tests use actual business logic with minimal external mocking
- **Container Lifecycle**: Proper container initialization, service resolution, and cleanup

### Cross-Layer Architecture Validation
- **Settings → Container → Services Flow**: Complete configuration and dependency resolution
- **Layer Coordination**: Tests validate proper interaction between all architectural layers
- **Interface Compliance**: Real-world validation that implementations meet interface contracts
- **Error Propagation**: Errors flow correctly across layer boundaries with proper recovery
- **Context Propagation**: Request context and correlation IDs maintained across container services
- **Performance Characteristics**: Container overhead validation and service performance testing

## Running Integration Tests - New Architecture

### Prerequisites

Install test dependencies:
```bash
pip install -r ../../requirements-test.txt
```

**Environment Configuration for Integration Testing**:
```bash
# Skip external service checks for integration testing
export SKIP_SERVICE_CHECKS=true

# Enable debug logging for integration test debugging
export LOG_LEVEL=DEBUG

# Set integration test configuration
export REDIS_HOST=192.168.0.111
export REDIS_PORT=30379
export CHROMADB_URL=http://chromadb.faultmaven.local:30080
```

### Basic Execution

**New Architecture Integration Tests**:
```bash
# Run new architecture workflow tests (18+ tests)
pytest tests/integration/test_new_architecture_workflows.py -v

# Run all integration tests with container patterns
SKIP_SERVICE_CHECKS=true pytest tests/integration/ -v

# Run from integration directory
cd tests/integration
SKIP_SERVICE_CHECKS=true pytest -v
```

**Specific Integration Test Categories**:
```bash
# End-to-end troubleshooting workflow testing
pytest tests/integration/test_new_architecture_workflows.py::TestEndToEndWorkflows -v

# Settings and container integration flow
pytest tests/integration/test_new_architecture_workflows.py::TestSettingsContainerServicesFlow -v

# Cross-layer error handling validation
pytest tests/integration/test_new_architecture_workflows.py::TestErrorHandlingAcrossLayers -v

# Interface compliance in production scenarios
pytest tests/integration/test_new_architecture_workflows.py::TestInterfaceComplianceInRealScenarios -v
```

### Mock Server Testing

Run mock server infrastructure tests:
```bash
# Note: Run individually to avoid port conflicts
pytest -k "mock_server" -v -s
```

### Environment Variables

Set environment variables for external service dependencies:
```bash
# Skip external service checks for testing
SKIP_SERVICE_CHECKS=true pytest tests/integration/ -v

# Enable debug logging
LOG_LEVEL=DEBUG pytest tests/integration/ -v
```

## Test Execution Notes

### Performance Characteristics
- **Individual Tests**: Complete in < 500ms each
- **Full Suite**: Completes in < 30 seconds
- **Memory Usage**: < 50MB peak during execution
- **Async Operations**: Proper async/await patterns with cleanup

### Common Patterns - New Architecture Integration

#### Container-Based Integration Testing
```python
from faultmaven.container import container
from faultmaven.config.settings import get_settings

@pytest.mark.asyncio
async def test_settings_container_services_integration():
    """Test complete Settings → Container → Services flow."""
    # Reset container for clean state
    container.reset()
    
    # Test with realistic environment configuration
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
        
        # Validate end-to-end service operation
        result = await agent_service.process_query(
            "Test troubleshooting query",
            "integration-test-session"
        )
        
        assert result.session_id == "integration-test-session"
        assert len(result.response) > 50
```

#### Interface Compliance Integration Testing
```python
@pytest.mark.asyncio
async def test_interface_compliance_real_scenario():
    """Test interface compliance in production-like scenarios."""
    container.reset()
    
    # Get services through container (interface resolution)
    agent_service = container.get_agent_service()
    knowledge_service = container.get_knowledge_service()
    
    # Validate services implement expected interfaces
    assert hasattr(agent_service, 'process_query')
    assert hasattr(knowledge_service, 'search_knowledge_base')
    
    # Test cross-service integration
    kb_result = await knowledge_service.search_knowledge_base(
        "database connection issues", limit=3
    )
    assert isinstance(kb_result, list)
    
    # Test agent service with knowledge integration
    agent_result = await agent_service.process_query(
        "Database connection problems",
        "interface-test-session"
    )
    assert agent_result.session_id == "interface-test-session"
```

## Troubleshooting

### Common Issues

**Async Test Failures**: Ensure proper async/await usage
```python
@pytest.mark.asyncio
async def test_async_operation():
    result = await async_function()
    assert result
```

**Mock Server Port Conflicts**: Run mock server tests individually
```bash
pytest test_file.py::test_specific_mock_test -v -s
```

**Logging Capture Issues**: Use proper logging fixtures
```python
def test_logging(logging_setup):
    log_capture = logging_setup
    # ... test logging behavior
    logs = log_capture.get_logs(level=logging.INFO)
```

### Debug Information

Enable verbose logging for troubleshooting:
```bash
# Maximum verbosity with debug logging
LOG_LEVEL=DEBUG pytest tests/integration/ -v -s --log-cli-level=DEBUG

# Capture and display async exceptions
pytest tests/integration/ -v -s --tb=long
```

## Architecture Integration

### Layer Boundaries
The integration tests validate that:

- **API Layer**: Properly handles requests and delegates to services
- **Service Layer**: Orchestrates business logic and coordinates infrastructure
- **Infrastructure Layer**: Manages external dependencies and technical concerns
- **Cross-Cutting Concerns**: Logging, security, and observability work across all layers

### Interface Compliance
All integration tests use the same interface-based dependency injection as the main application:

- **Service Dependencies**: Injected via interfaces (ILLMProvider, ISanitizer, etc.)
- **Mock Implementations**: Implement proper interface contracts
- **Real Business Logic**: Services use actual business logic with mocked infrastructure
- **Error Propagation**: Proper exception handling and error boundaries

## Maintenance

### Adding New Integration Tests - Architecture Focus

New integration tests should focus on:
1. **Container Integration**: Testing service resolution and lifecycle through DI container
2. **Cross-Layer Communication**: Validating proper layer interaction and data flow patterns
3. **Interface Compliance**: Testing that all services properly implement interface contracts
4. **End-to-End Architecture Workflows**: Complete user scenarios through all architectural layers
5. **Settings Integration**: Testing configuration propagation through container to services
6. **Error Handling Integration**: Cross-layer error propagation and graceful degradation
7. **Performance Integration**: Container overhead and cross-layer performance characteristics

### Maintenance Philosophy - New Architecture

1. **Architecture Compliance First**: Validate Clean Architecture principles in all integration tests
2. **Container Lifecycle Management**: Ensure proper container reset and service isolation
3. **Interface-Based Testing**: Focus on interface compliance rather than implementation details
4. **Cross-Layer Coordination**: Test complete workflows through all architectural layers
5. **Business Value Integration**: Validate end-to-end business scenarios with realistic data
6. **Performance Awareness**: Monitor container overhead and cross-layer performance impacts
7. **Error Scenario Coverage**: Test all error propagation and recovery patterns across layers

## Contributing

When adding new integration tests:

1. **Container-First Approach**: Always use container patterns for service resolution and state management
2. **Interface Compliance**: Mock interfaces rather than concrete implementations
3. **Environment Isolation**: Use clean environment fixtures for proper test isolation
4. **Cross-Layer Validation**: Test complete workflows through all architectural layers
5. **Error Scenario Coverage**: Include error handling and graceful degradation testing
6. **Performance Consideration**: Monitor container overhead and service performance
7. **Architecture Alignment**: Ensure tests validate Clean Architecture principles
8. **Documentation**: Clearly document architecture patterns and integration flows being tested

## Resources

- [Main Test Suite Documentation](../README.md)
- [Test Doubles and Utilities](../test_doubles.py)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [FaultMaven Clean Architecture Guide](../../docs/architecture/)