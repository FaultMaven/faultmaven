# FaultMaven Test Suite

Comprehensive test suite with 140+ test files organized for CI/CD pipelines.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures and test configuration
├── utils.py                 # Test utilities (ID generators, helpers)
├── unit/                    # Unit tests (~100 files)
│   ├── api/                 # API layer tests (routes, middleware, models)
│   ├── architecture/        # Architecture validation tests
│   ├── config/              # Configuration and settings tests
│   ├── container/           # Dependency injection container tests
│   ├── core/                # Core domain logic tests
│   ├── infrastructure/      # Infrastructure adapter tests
│   ├── investigation/       # Investigation engine tests
│   ├── models/              # Domain model tests
│   ├── modules/             # Feature module tests (agent, evidence)
│   ├── prompts/             # Prompt template tests
│   ├── providers/           # Provider implementation tests
│   ├── services/            # Service layer tests
│   ├── tools/               # Agent tool tests
│   └── utils/               # Utility function tests
├── integration/             # Integration tests (~25 files)
│   ├── api/                 # API integration tests
│   ├── conftest.py          # Integration-specific fixtures
│   ├── mock_servers.py      # Mock API servers for external services
│   └── test_*.py            # Cross-layer workflow tests
├── infrastructure/          # Infrastructure-specific tests
│   ├── knowledge/           # Knowledge base infrastructure
│   ├── logging/             # Logging infrastructure
│   └── test_*.py            # LLM, vector store, Redis tests
├── benchmarks/              # Performance benchmarks (baseline tracking)
│   ├── conftest.py          # Benchmark fixtures
│   └── test_*.py            # Operation benchmarks
├── performance/             # Performance overhead tests
│   ├── test_context_overhead.py
│   └── test_logging_overhead.py
├── health/                  # Infrastructure smoke tests
│   └── test_docker_health.py
└── load/                    # Load tests (Locust)
    └── locustfile.py        # Stress test scenarios
```

## Quick Start

```bash
# Run all tests
./faultmaven.sh test

# Run unit tests only
./faultmaven.sh test --unit

# Run with coverage
./faultmaven.sh test --coverage

# Run in parallel
./faultmaven.sh test --parallel
```

## CI/CD Pipeline Modes

The test runner supports predefined CI/CD pipeline configurations:

```bash
# Fast CI (unit tests, parallel) - for PR checks
./faultmaven.sh test --ci

# Full CI (unit + integration + infrastructure) - for merge checks
./faultmaven.sh test --ci-full

# Nightly (all tests including benchmarks)
./faultmaven.sh test --ci-nightly

# Enterprise (tests requiring Redis, PostgreSQL)
./faultmaven.sh test --ci-enterprise
```

## Test Categories

| Category | Command | Description |
|----------|---------|-------------|
| Unit | `--unit` | Fast, isolated unit tests |
| Integration | `--integration` | Cross-layer workflow tests |
| Infrastructure | `--infrastructure` | External service integration |
| Benchmarks | `--benchmarks` | Performance baseline tests |
| Performance | `--performance` | Overhead validation tests |
| Health | `--health` | Infrastructure smoke tests |

## Running Specific Tests

```bash
# By keyword
./faultmaven.sh test -k "test_case"

# By marker
./faultmaven.sh test -m "security"

# By directory
./faultmaven.sh test --dir tests/unit/services

# Single file
./faultmaven.sh test tests/unit/services/test_auth_service.py

# Stop on first failure
./faultmaven.sh test --fail-fast
```

## Pytest Markers

| Marker | Description |
|--------|-------------|
| `unit` | Unit tests |
| `integration` | Integration tests |
| `slow` | Long-running tests (excluded from fast CI) |
| `enterprise` | Requires Redis, PostgreSQL, etc. |
| `security` | Security-focused tests |
| `benchmark` | Performance benchmarks |
| `performance` | Performance overhead tests |

## Load Testing

Load tests use Locust for stress testing:

```bash
# Interactive mode (web UI)
./faultmaven.sh test load

# Headless mode
./faultmaven.sh test load --headless --users 50 --run-time 60s

# With CSV output
./faultmaven.sh test load --headless --csv results
```

## Background Execution

For CI pipelines or long-running test suites (not typical local development):

```bash
# Run in background (useful for CI or full test suites)
./faultmaven.sh test --ci-full --daemon

# Check status
./faultmaven.sh test status

# View logs
tail -f .faultmaven/tests.log

# Stop background tests
./faultmaven.sh test stop
```

## Coverage Reports

```bash
# Generate coverage report
./faultmaven.sh test --coverage

# With minimum threshold
./faultmaven.sh test --coverage --coverage-fail-under 75

# View HTML report
open htmlcov/index.html
```

## CI Output

```bash
# Generate JUnit XML for CI systems
./faultmaven.sh test --ci --junit-xml test-results.xml
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `SKIP_SERVICE_CHECKS` | Skip external service availability checks |
| `RUN_PERFORMANCE_TESTS` | Enable performance/benchmark tests |
| `LOG_LEVEL` | Set logging level (DEBUG, INFO, etc.) |

## Writing Tests

### Test Fixtures

Common fixtures are available in `conftest.py`:

```python
def test_with_container(reset_container):
    """Test with clean DI container state."""
    service = reset_container.get_agent_service()
    assert service is not None

def test_with_sample_data(sample_case, sample_session_context):
    """Test with pre-built sample data."""
    assert sample_case.case_id.startswith("case_")
```

### Async Tests

```python
import pytest

@pytest.mark.asyncio
async def test_async_operation():
    result = await some_async_function()
    assert result is not None
```

### Parameterized Tests

```python
@pytest.mark.parametrize("provider,expected", [
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("fireworks", "Fireworks"),
])
def test_provider_names(provider, expected):
    assert get_provider_name(provider) == expected
```

## Troubleshooting

### Import Errors

```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### Async Test Issues

Ensure `pytest-asyncio` is installed and use `@pytest.mark.asyncio`.

### Docker Service Issues

```bash
docker-compose up -d
docker-compose ps
```

### Parallel Execution Issues

```bash
# Install pytest-xdist
pip install pytest-xdist

# Run without parallelism
./faultmaven.sh test --no-parallel
```
