# FaultMaven Integration Tests

This directory contains integration tests that validate cross-layer functionality and complete system workflows, using **Clean Architecture** and **dependency injection** patterns.

## Overview

This directory contains integration test infrastructure focused on:

1. **Container-Based Integration** - Cross-layer validation using dependency injection container
2. **Interface Compliance Testing** - Integration testing with proper interface contracts
3. **Cross-Layer Test Utilities** - Shared fixtures and test infrastructure with container support

## Current Test Structure

The integration directory contains:

#### `test_architectural_compliance.py` ✅ NEW (2025-10-24)
Comprehensive spec compliance testing with 17 tests validating [case-and-session-concepts.md v2.0](../../docs/architecture/case-and-session/case-and-session-concepts.md):

- **Session Resumption Tests** (3 tests) - client_id-based session resumption validation
- **Multi-Device Support Tests** (2 tests) - Concurrent sessions on multiple devices
- **Case Access Tests** (3 tests) - Case persistence and cross-session access
- **Forbidden Field Validation Tests** (4 tests) - Session updates reject case data
- **Security Tests** (3 tests) - owner_id requirements and ownership enforcement
- **Field Compliance Tests** (2 tests) - Multi-device fields in SessionContext

**Setup:** a reachable Redis, given through the environment (`REDIS_HOST`, `REDIS_PORT`, and `REDIS_PASSWORD` if it requires auth); the suite never reads `.env` (`tests/conftest.py` blocks it). The tests are `cloud`-marked. Until #2081 is fixed, the 16 that take `session_service` skip whether or not Redis is reachable: the fixture never builds its store.

**Run tests:**
```bash
# All compliance tests
REDIS_HOST=localhost REDIS_PORT=6379 pytest tests/integration/test_architectural_compliance.py -v

# Specific test
.venv/bin/pytest tests/integration/test_architectural_compliance.py::TestArchitecturalCompliance::test_session_resumption_with_same_client_id -v
```

### Additional Integration Tests

#### `test_kb_ingestion_and_indexing.py`
Knowledge base integration testing with vector store operations

#### `test_readiness_and_redis.py`
System readiness and Redis integration validation

### Infrastructure Files

#### `mock_servers.py`
`get_free_port()`: a free port for a test that serves an app on loopback.

#### `conftest.py`
Integration test configuration and fixtures:

- **All-skipped guard**: a session that selects tests and skips every one fails
- **Served-app teardown**: each test ends the protection middleware of the apps it served
- **Service fixtures**: `session_service` (real Redis; it currently always skips, #2081) and `case_service` (the DI container's)
- **Database isolation**: `in_memory_database_url` points `DATABASE_URL` at an in-memory SQLite for one test

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

## Running Integration Tests - New Architecture

### Prerequisites

Install test dependencies:
```bash
# from the repository root: the lockfile pins the dependencies, not the package
pip install -r requirements/test.txt
pip install -e . --no-deps
```

**No environment variables are required.** These tests run in-process — they
drive the application through `TestClient` and real repositories against
throwaway SQLite databases, not through a live HTTP server. There is no
"wait for the backend to come up" step to configure.

Optional settings:
```bash
# Enable debug logging for integration test debugging
export LOG_LEVEL=DEBUG

# Point the Redis-backed suites at a real Redis (REDIS_PASSWORD if it requires auth).
# They self-skip without one.
export REDIS_HOST=localhost
export REDIS_PORT=6379

# Point the postgres-marked suites at a scratch PostgreSQL
export DATABASE_URL=postgresql://…
```

`SKIP_SERVICE_CHECKS` is **not** a test-runner switch. It is a live application
config knob (read by the DI container providers and the middleware wiring in
`faultmaven/bootstrap/middleware.py`), so a handful of suites that boot an in-process app set it
deliberately. Setting it no longer changes which tests run.

### Basic Execution

**New Architecture Integration Tests**:
```bash
# Run all integration tests
pytest tests/integration/ -v

# Run a single suite
pytest tests/integration/test_alembic_migrations.py -v

# Run from integration directory
cd tests/integration
pytest -v
```

### An all-skipped run is a failure

If a session selects tests and skips **every** one of them, this suite forces a
non-zero exit and prints the distinct skip reasons. A green run must mean tests
actually executed — a missing service is not allowed to silently turn the tree
into no-ops.

The practical consequence: running a suite whose backing service is absent —
for example `pytest tests/integration/test_postgresql_repository_roundtrip.py`
with no PostgreSQL — now **fails** instead of reporting green with skips.
Provide the service (a real `DATABASE_URL`, a reachable Redis) or narrow the
selection so the run stops claiming to cover it. Mixed runs, where some tests
pass and environment-dependent ones skip, stay green.

### Environment Variables

None are required. Optional ones:
```bash
# Enable debug logging
LOG_LEVEL=DEBUG pytest tests/integration/ -v

# Back the Redis-dependent suites with a real Redis (REDIS_PASSWORD if it requires auth)
REDIS_HOST=localhost REDIS_PORT=6379 pytest tests/integration/ -v
```

## Test Execution Notes

### Performance Characteristics
- **Individual Tests**: Complete in < 500ms each
- **Full Suite**: Completes in < 30 seconds
- **Memory Usage**: < 50MB peak during execution
- **Async Operations**: Proper async/await patterns with cleanup

## Troubleshooting

### Common Issues

**Async Test Failures**: Ensure proper async/await usage
```python
@pytest.mark.asyncio
async def test_async_operation():
    result = await async_function()
    assert result
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
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [FaultMaven Clean Architecture Guide](../../docs/architecture/)
