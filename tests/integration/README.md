# FaultMaven Integration Tests

This directory contains integration tests that validate cross-layer functionality and complete system workflows with minimal external dependencies.

## Overview

This directory contains integration test infrastructure focused on:

1. **Mock API Infrastructure** - External service simulation for testing without dependencies
2. **Cross-Layer Test Utilities** - Shared fixtures and test infrastructure

## Current Test Structure

The integration directory contains shared test infrastructure and utilities:

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

## Test Philosophy

### Minimal External Dependencies
- **Real Business Logic**: Tests use actual business logic where possible
- **External Boundary Mocking**: Only mock true external systems (APIs, databases)
- **Interface Compliance**: All mocks implement proper interface contracts
- **Realistic Behavior**: Mocks provide meaningful responses, not just pass-through

### Cross-Layer Integration
- **Layer Coordination**: Tests validate proper interaction between architectural layers
- **Context Propagation**: Request context and correlation IDs flow correctly
- **Error Handling**: Errors propagate appropriately across layer boundaries
- **Performance Characteristics**: Real timing and resource usage validation

## Running Integration Tests

### Prerequisites

Install test dependencies:
```bash
pip install -r ../../requirements-test.txt
```

### Basic Execution

Run all integration tests:
```bash
# From project root
pytest tests/integration/ -v

# From integration directory
cd tests/integration
pytest -v
```

Integration testing primarily occurs through mock API infrastructure and shared test utilities.

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

### Common Patterns

#### Mock API Testing
```python
@pytest.mark.asyncio
async def test_llm_provider_mock(mock_servers):
    """Test LLM provider with mock API."""
    # Mock API server provides realistic responses
    router = LLMRouter()
    response = await router.route("test query")
    assert len(response) > 700
    assert "test" in response.lower()
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

### Adding New Integration Tests

New integration tests should focus on:
1. **External API Simulation**: Mock servers for testing without external dependencies
2. **Cross-Service Integration**: Testing service interactions through proper interfaces
3. **End-to-End Workflows**: Realistic user scenarios with minimal mocking
4. **Infrastructure Testing**: External service integration with fallback behavior

### Maintenance Philosophy

1. **Business Value First**: Only add tests that validate real business scenarios
2. **Service Layer Focus**: Most integration testing happens at service layer
3. **External Boundary Testing**: Integration tests focus on external service interactions
4. **Maintainability**: Favor simple, focused tests over complex integration scenarios

## Contributing

When adding new integration tests:

1. **Follow existing patterns** for async testing and logging verification
2. **Use provided fixtures** from `conftest.py` for consistent setup
3. **Test realistic scenarios** that reflect actual application usage
4. **Document test purpose** clearly in docstrings and comments
5. **Ensure proper cleanup** to prevent test interference

## Resources

- [Main Test Suite Documentation](../README.md)
- [Test Doubles and Utilities](../test_doubles.py)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [FaultMaven Clean Architecture Guide](../../docs/architecture/)