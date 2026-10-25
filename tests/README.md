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
python scripts/tests.py

# Run unit tests only
python scripts/tests.py --unit

# Run with coverage
python scripts/tests.py --coverage

# Run in parallel
python scripts/tests.py --parallel
```

## Preset Modes

The runner's preset selections. CI does not use them: its jobs run `pytest`
directly (`.github/workflows/ci-cd.yml`).

```bash
# Unit tests, parallel, without the slow and cloud tests
python scripts/tests.py --ci

# Unit + integration + infrastructure, parallel, without the slow and cloud tests
python scripts/tests.py --ci-full

# Every test but the cloud ones, benchmarks included, one process
python scripts/tests.py --ci-nightly

# The tests marked `cloud`, which need a Redis server or a PostgreSQL DATABASE_URL
python scripts/tests.py --ci-cloud

# The PostgreSQL suite (marked `postgres`), as CI runs it, with DATABASE_URL
# naming a PostgreSQL server on localhost: the tests expect the schema at head
alembic upgrade head
SKIP_SERVICE_CHECKS=true pytest tests/ -m postgres
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
python scripts/tests.py -k "test_case"

# By marker
python scripts/tests.py -m "security"

# By directory
python scripts/tests.py --dir tests/unit/services

# Single file
pytest tests/unit/services/test_base_service.py

# Stop on first failure
python scripts/tests.py --fail-fast
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
python scripts/tests.py load

# Headless mode
python scripts/tests.py load --headless --users 50 --run-time 60s

# With CSV output
python scripts/tests.py load --headless --csv results
```

## Background Execution

For CI pipelines or long-running test suites (not typical local development):

```bash
# Run in background (useful for CI or full test suites)
python scripts/tests.py --ci-full --daemon

# Check status
python scripts/tests.py status

# View logs
tail -f .faultmaven/tests.log

# Stop background tests
python scripts/tests.py stop
```

## Coverage Reports

```bash
# Generate coverage report
python scripts/tests.py --coverage

# With minimum threshold
python scripts/tests.py --coverage --coverage-fail-under 75

# View HTML report
open htmlcov/index.html
```

## CI Output

```bash
# Generate JUnit XML for CI systems
python scripts/tests.py --ci --junit-xml test-results.xml
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

### Tests That Need Redis or PostgreSQL

The `cloud` and `postgres` tests skip unless `REDIS_URL` (or `REDIS_HOST`) or a
PostgreSQL `DATABASE_URL` names a server. The repository's `docker-compose.yml`
starts neither (it runs the API and the Dashboard), so point those variables at
a server you run yourself on localhost: the test network guard
(`tests/network_guard.py`) refuses every connection to another host.

### Parallel Execution Issues

```bash
# Install pytest-xdist
pip install pytest-xdist

# Run without parallelism
python scripts/tests.py --no-parallel
```
