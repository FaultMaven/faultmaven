# Testing Documentation

Comprehensive testing documentation for FaultMaven contributors.

## Overview

FaultMaven maintains **71%+ test coverage** with a strong emphasis on test-first development. All new code requires tests before merging.

## Testing Documents

### Core Standards

- **[Testing Standards](./standards.md)** - Testing requirements, coverage targets, and mandatory practices
  - Coverage requirements (71%+ overall, 80%+ for new code)
  - Test categories (unit, integration, performance, security)
  - PR testing checklist
  - Exemption process

### Testing Guides

- **[Testing Guide](./guide.md)** - Practical guide to writing and running tests
  - Test structure and organization
  - Running tests with pytest
  - Async testing patterns
  - Mocking and fixtures

- **[Test Patterns](./patterns.md)** - Common testing patterns and best practices
  - Unit test patterns
  - Integration test patterns
  - Async test patterns
  - Mocking strategies
  - Test data management

### Specialized Testing

- **[Architecture Testing](./architecture.md)** - Testing architectural boundaries and dependencies
  - Import linting and dependency rules
  - Module boundary testing
  - Layer isolation testing
  - Contract testing

- **[Investigation Testing](./investigation.md)** - Testing investigation workflows and agents
  - Testing investigation milestones
  - Agent interaction testing
  - OODA loop testing
  - State machine testing

- **[Performance Testing](./performance.md)** - Performance and load testing strategies
  - Load testing with Locust
  - Performance benchmarks
  - Profiling and optimization
  - Resource usage testing

## Quick Start

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=faultmaven --cov-report=html

# Run specific test file
pytest tests/unit/test_example.py

# Run specific test class or method
pytest tests/unit/test_example.py::TestClass::test_method

# Run tests with verbose output
pytest -v

# Run tests matching pattern
pytest -k "test_pattern"
```

### Writing Your First Test

```python
import pytest
from faultmaven.services.example_service import ExampleService

class TestExampleService:
    """Tests for ExampleService"""

    @pytest.fixture
    def service(self):
        """Fixture providing ExampleService instance"""
        return ExampleService()

    def test_example_method_success(self, service):
        """Test example_method with valid input"""
        # Arrange
        input_data = {"key": "value"}

        # Act
        result = service.example_method(input_data)

        # Assert
        assert result.success is True
        assert result.data == expected_output
```

## Testing Requirements

### Before Merging PRs

- ✅ All tests pass (`pytest`)
- ✅ Coverage maintained or improved
- ✅ New code has 80%+ coverage
- ✅ Integration tests for new features
- ✅ No skipped tests without justification

### Test Categories Required

| Code Change | Required Tests |
|-------------|----------------|
| New feature | Unit + Integration + (Performance if applicable) |
| Bug fix | Regression test demonstrating the bug + fix |
| Refactoring | Existing tests pass + no coverage decrease |
| API endpoint | Integration test + security test |
| Database schema | Migration test + rollback test |
| LLM integration | Unit test with mocked LLM + integration test |

## Testing Philosophy

FaultMaven follows these testing principles:

1. **Test-First Development** - Write tests before or during implementation
2. **No Code Without Tests** - All new code requires tests (no exceptions without approval)
3. **Maintain Coverage** - Never decrease overall coverage below 71%
4. **Test Behavior, Not Implementation** - Focus on what code does, not how
5. **Fast Tests** - Keep unit tests fast (<1s), use mocking for external dependencies
6. **Isolated Tests** - Each test should be independent and repeatable

## Test Organization

```
tests/
├── unit/                  # Fast, isolated unit tests
│   ├── services/          # Service layer tests
│   ├── models/            # Model tests
│   └── utils/             # Utility function tests
├── integration/           # Tests with real dependencies
│   ├── api/               # API endpoint tests
│   ├── database/          # Database integration tests
│   └── external/          # External service integration
└── performance/           # Performance and load tests
    └── locust/            # Locust performance tests
```

## Common Testing Scenarios

### Testing Async Code

```python
import pytest

@pytest.mark.asyncio
async def test_async_function():
    result = await async_function()
    assert result == expected
```

### Mocking External Services

```python
from unittest.mock import Mock, patch

@patch('faultmaven.services.external_service.ExternalAPI')
def test_with_mocked_service(mock_api):
    mock_api.return_value.fetch_data.return_value = {"data": "mocked"}
    result = service_using_api()
    assert result == expected
```

### Testing Database Operations

```python
@pytest.fixture
async def db_session():
    """Fixture providing test database session"""
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()  # Rollback after each test

async def test_database_operation(db_session):
    # Test database operation
    entity = Entity(name="test")
    db_session.add(entity)
    await db_session.flush()

    assert entity.id is not None
```

## Related Documentation

- [Development Standards](../README.md) - General development standards
- [Environment Variables](../environment-variables.md) - Test environment configuration
- [Contributing Guide](../../CONTRIBUTING.md) - Contribution workflow

---

**Last Updated**: 2026-01-22
