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
  - Milestone-based investigation testing
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

### Line-level coverage comparisons

The coverage total is reliable at the default settings, but the per-line data is not. SQLAlchemy's async engine switches greenlets on every database call, and coverage misattributes the lines that run across those switches (#2206). It records lines against the wrong file, some of them past that file's end, and misses real ones. Measured on `tests/integration/modules/knowledge`, a default run against a greenlet-aware one:

- 118 lines recorded that never ran;
- 24 lines missed that did run;
- 8 files affected;
- 92 recorded lines past the end of their file, in 7 of those files.

So whenever *which lines* are covered decides something, opt in. That includes a refactor's "no coverage decrease" set comparison and a review's "is this line covered".

1. Confirm the setting on each side of the comparison. A checkout older than this setting ignores the variable and prints `[]`; for such a base, add the same `concurrency` line to its `pyproject.toml` in a scratch worktree.
2. Run.
3. Check the raw data: no recorded line may lie past the end of its file.

```bash
export FM_COVERAGE_CONCURRENCY=greenlet
python -c "import coverage; print(coverage.Coverage().config.concurrency)"   # ['thread', 'greenlet']
pytest tests/integration/modules/knowledge --cov=faultmaven --cov-report=
python -c "
import coverage
cov = coverage.Coverage(); cov.load(); d = cov.get_data()   # honours COVERAGE_FILE
assert d.measured_files(), 'no coverage data: wrong COVERAGE_FILE or directory?'
bad = [(f, n) for f in d.measured_files() for n in d.lines(f) if n > sum(1 for _ in open(f))]
assert not bad, bad[:5]
"
```

Read the raw data, as the snippet does. `coverage report` and `coverage json` keep only statement lines, so they can never show a line past the end of a file. The check works per run, not per file. One such line anywhere means the run misattributed, but a file without one can still be off; the run above has a file off by 6 lines with none of its own.

**CI leaves it off.** Greenlet tracking cost +39% to +52% on database-heavy tests. CI's coverage gate is `--fail-under`. The misattribution moves the total slightly (13 of 56,591 missed statements in one measurement), but it cannot flip 80% against a threshold of 50%. Both test jobs also print `--cov-report=term-missing`, a per-line listing that carries the same error, so do not read line-level conclusions from it.

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
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base, EnterpriseModel

@pytest.fixture
async def db_session():
    """Fixture providing a session on a throwaway in-memory SQLite"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
        await session.rollback()  # Rollback after each test
    await engine.dispose()

async def test_database_operation(db_session):
    # Test database operation
    entity = EnterpriseModel(enterprise_id="ent_test", name="test", slug="test")
    db_session.add(entity)
    await db_session.flush()

    name = await db_session.scalar(
        select(EnterpriseModel.name).where(EnterpriseModel.enterprise_id == "ent_test")
    )
    assert name == "test"
```

## Related Documentation

- [Development Standards](../README.md) - General development standards
- [Environment Variables](../environment-variables.md) - Test environment configuration
- [Contributing Guide](../../CONTRIBUTING.md) - Contribution workflow

---

**Last Updated**: 2026-01-22
