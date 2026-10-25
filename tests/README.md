# FaultMaven Test Suite

Comprehensive test suite organized for CI/CD pipelines.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures and test configuration
├── utils.py                 # Test utilities (ID generators, helpers)
├── unit/                    # Unit tests
│   ├── api/                 # API layer tests (routes, middleware, models)
│   ├── architecture/        # Architecture validation tests
│   ├── bootstrap/           # Startup: migrations gate, KB pack ingestion, lifespan
│   ├── ci/                  # CI gates: docs-only classifier, benchmark calibration
│   ├── cli/                 # Operator console (fm-*) commands
│   ├── config/              # Configuration and settings tests
│   ├── container/           # Dependency injection container tests
│   ├── core/                # Core domain logic tests (investigation engine, prompts)
│   ├── infrastructure/      # Infrastructure adapter tests
│   ├── jobs/                # Background job tests
│   ├── models/              # Domain model tests
│   ├── modules/             # Feature module tests (agent and its tools, auth, case, evidence, knowledge, preprocessing, report)
│   ├── providers/           # Provider implementation tests
│   ├── scripts/             # Dev and maintenance script tests
│   ├── security/            # Security tests
│   ├── services/            # Service layer tests
│   └── utils/               # Utility function tests
├── integration/             # Integration tests
│   ├── api/                 # API integration tests
│   ├── conftest.py          # Integration-specific fixtures
│   ├── mock_servers.py      # get_free_port(), for tests that serve on loopback
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
| `slow` | Long-running tests (the required Test Standalone/Test Cloud jobs run them; `scripts/tests.py --ci` skips them) |
| `cloud` | Cloud tests requiring Redis, PostgreSQL |
| `security` | Security-focused tests |
| `benchmark` | Performance benchmarks |
| `performance` | Wall-clock tests: `tests/performance` and the defensive growth checks (excluded from the required jobs; run in `benchmarks.yml`) |

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
| `SKIP_SERVICE_CHECKS` | Application config knob, not a test switch — relaxes service-availability checks in the app the tests boot in-process. It does not gate which tests run |
| `LOG_LEVEL` | Set logging level (DEBUG, INFO, etc.) |

## Writing Tests

### Test Fixtures

Common fixtures are available in `conftest.py`:

```python
async def test_with_container(reset_container):
    """Test with clean DI container state; the fixture shuts it down after."""
    await reset_container.initialize()  # a getter never composes it (#2249)
    service = reset_container.get_agent_service()
    assert service is not None

def test_with_started_app(booted_app_client):
    """Test against the started app, shared by the module's tests."""
    assert booted_app_client.get("/health").status_code == 200
```

Sample data comes from plain keyword-override builders, not fixtures:

```python
from tests.case_builders import inquiry_case

def test_with_sample_data():
    """Test with a pre-built case (tests/case_builders.py)."""
    case = inquiry_case(statement="Pods crash on start")
    assert case.case_id.startswith("case_")
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
from faultmaven.infrastructure.llm.providers.registry import configuration_env_var

@pytest.mark.parametrize("provider,expected", [
    ("openai", "OPENAI_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("fireworks", "FIREWORKS_API_KEY"),
])
def test_provider_env_vars(provider, expected):
    assert configuration_env_var(provider) == expected
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
