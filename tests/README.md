# FaultMaven Test Suite

This directory contains the comprehensive test suite for the FaultMaven backend. The tests are designed to validate the correctness and resilience of each module in isolation.

## Test Structure

The test directory mirrors the source code structure:

```
tests/
├── __init__.py
├── conftest.py                 # Shared fixtures and configuration
├── security/
│   └── test_redaction.py      # Data sanitization tests
├── data_processing/
│   ├── test_classifier.py     # Data classification tests
│   └── test_log_processor.py  # Log processing tests
├── agent/
│   └── tools/
│       └── test_knowledge_base.py  # Knowledge base tool tests
├── api/
│   └── test_data_ingestion.py # API endpoint tests
├── llm/
│   └── test_router.py         # LLM router tests
└── test_session_management.py # Session management tests
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
- End-to-end workflows
- Cross-module interactions
- Database operations

## Running Tests

### Prerequisites

Install test dependencies:
```bash
pip install -r requirements-test.txt
```

### Basic Test Execution

Run all tests:
```bash
pytest
```

Run with coverage:
```bash
pytest --cov=faultmaven --cov-report=html
```

### Test Categories

Run specific test categories:
```bash
# Unit tests only
pytest -m unit

# Security tests only
pytest -m security

# API tests only
pytest -m api

# Data processing tests only
pytest -m data_processing
```

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

### Using the Test Runner Script

The `run_tests.py` script provides convenient test execution options:

```bash
# Run all tests and checks
python run_tests.py --all

# Run only unit tests
python run_tests.py --unit

# Run with coverage
python run_tests.py --coverage --html

# Run linting only
python run_tests.py --lint

# Run type checking
python run_tests.py --type-check
```

## Test Design Principles

### Isolation
- All external dependencies are mocked
- Tests are independent and can run in any order
- No shared state between tests

### Coverage
- Each function has multiple test cases
- Edge cases and error conditions are tested
- Parameterized tests for efficient testing

### Mocking Strategy
- **External APIs**: LLM providers, ChromaDB, external services
- **File System**: File operations, temporary files
- **Time**: Date/time operations for session management
- **Network**: HTTP requests, database connections

### Test Data
- Realistic but safe test data
- No production credentials or sensitive information
- Consistent test fixtures in `conftest.py`

## Test Examples

### Unit Test Example
```python
def test_data_classification_system_logs(classifier):
    """Test classification of system logs."""
    text = "2024-01-01 12:00:00 ERROR Database connection failed"
    result = classifier.classify(text)
    assert result == DataType.SYSTEM_LOGS
```

### API Test Example
```python
@pytest.mark.asyncio
async def test_upload_data_success(async_client, mock_dependencies):
    """Test successful data upload."""
    response = await async_client.post(
        "/data",
        files={"file": ("test.log", io.BytesIO(b"test content"))},
        data={"session_id": "test-session"}
    )
    assert response.status_code == 200
```

### Mock Example
```python
@pytest.fixture
def mock_llm_router():
    """Mock LLM router for testing."""
    router = Mock()
    router.route.return_value = "Mocked response"
    return router
```

## Coverage Requirements

- **Minimum Coverage**: 80%
- **Critical Paths**: 95%
- **Error Handling**: 100%

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

## Best Practices

### Writing Tests
1. **Descriptive Names**: Test names should clearly describe what is being tested
2. **Arrange-Act-Assert**: Structure tests with clear sections
3. **One Assertion**: Each test should verify one specific behavior
4. **Parameterized Tests**: Use `@pytest.mark.parametrize` for multiple test cases
5. **Fixtures**: Reuse common test setup with fixtures

### Test Maintenance
1. **Keep Tests Fast**: Mock external dependencies
2. **Update Tests**: When changing implementation, update corresponding tests
3. **Review Coverage**: Regularly check coverage reports
4. **Refactor Tests**: Keep tests clean and maintainable

### Common Patterns
```python
# Parameterized testing
@pytest.mark.parametrize("input,expected", [
    ("test1", "result1"),
    ("test2", "result2"),
])

# Async testing
@pytest.mark.asyncio
async def test_async_function():
    result = await async_function()
    assert result == expected

# Mocking external dependencies
@patch('module.external_service')
def test_with_mock(mock_service):
    mock_service.return_value = "mocked_result"
    # test implementation
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

**Mock Not Working**: Check import paths and mock placement
```python
@patch('faultmaven.module.ClassName')
def test_with_mock(mock_class):
    # test code
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

## Contributing

When adding new tests:

1. Follow the existing test structure
2. Add appropriate markers
3. Update coverage requirements if needed
4. Document new test patterns
5. Ensure all tests pass before submitting

## Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [pytest-mock](https://pytest-mock.readthedocs.io/)
- [Coverage.py](https://coverage.readthedocs.io/) 