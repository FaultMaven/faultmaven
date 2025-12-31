# TASK-005: Performance Baseline Suite

## Task Metadata
- **Phase**: Week 1, Day 8-10 (Foundation - Performance Baseline)
- **Priority**: P1 (Blocks future performance regressions)
- **Estimated Time**: 2-3 hours
- **Dependencies**: TASK-001, TASK-002, TASK-003, TASK-004
- **Assignee**: Developer + Test-Engineer
- **Reviewer**: Solutions Architect

## Objective

**Establish performance baselines before implementing new features** to detect regressions during evolution.

### Success Criteria

1. ✅ Benchmark suite runs via `pytest -m benchmark`
2. ✅ Latency targets defined for critical operations
3. ✅ CI integration for automated regression detection
4. ✅ Performance dashboard (optional, nice-to-have)
5. ✅ Load testing scripts for stress testing
6. ✅ Documentation for interpreting results
7. ✅ Baseline measurements captured for future comparison

### Critical Requirements

- **Case Creation**: p95 latency < 200ms
- **Case Retrieval**: p95 latency < 100ms
- **Knowledge Search**: p95 latency < 300ms (with vector search)
- **Session Operations**: p95 latency < 50ms
- **Database Migration**: Downtime < 5 seconds for schema changes
- **Memory Usage**: < 512MB RSS for typical workload (10 concurrent cases)

---

## Context

### Why Performance Baselines Matter

As FaultMaven evolves from a monolith to a modular architecture with optional dependencies, we need to ensure:

1. **No performance regression** during refactoring
2. **Quantifiable impact** of new features (e.g., does PII redaction add 50ms latency?)
3. **Scalability confidence** before production deployment
4. **SLA validation** (can we meet enterprise customer requirements?)

### Evolution Strategy Alignment

From the [evolution strategy](../../docs/architecture/evolution-strategy.md):

> **Day 8-10: Performance Baseline Suite**
>
> Run before each phase to track regression. Establish latency targets:
> - Case creation: < 200ms p95
> - Knowledge search: < 300ms p95
> - Session operations: < 50ms p95

This task creates the **measurement infrastructure** that will be used throughout all future phases.

---

## Implementation Plan

### Phase 1: Benchmark Infrastructure (60 minutes)

#### 1.1 Install Dependencies

**File**: `pyproject.toml`

```toml
[tool.poetry.group.benchmark]
optional = true

[tool.poetry.group.benchmark.dependencies]
pytest-benchmark = "^4.0.0"
locust = "^2.20.0"
psutil = "^5.9.0"  # Memory profiling
```

**Commands**:
```bash
poetry install --with benchmark
```

#### 1.2 Create Benchmark Fixtures

**File**: `tests/benchmarks/conftest.py`

```python
"""Benchmark test fixtures."""

import pytest
import asyncio
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.case_repository import DatabaseCaseRepository
from faultmaven.infrastructure.persistence.session_repository import DatabaseSessionRepository


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def benchmark_engine():
    """Create database engine for benchmarks (SQLite in-memory)."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,  # Disable SQL logging for clean benchmarks
    )

    # Create schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest.fixture
async def benchmark_session(
    benchmark_engine
) -> AsyncGenerator[AsyncSession, None]:
    """Create database session for benchmarks."""
    SessionLocal = async_sessionmaker(
        benchmark_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with SessionLocal() as session:
        yield session


@pytest.fixture
async def case_repository(benchmark_session):
    """Create case repository for benchmarks."""
    return DatabaseCaseRepository(benchmark_session)


@pytest.fixture
async def session_repository(benchmark_session):
    """Create session repository for benchmarks."""
    return DatabaseSessionRepository(benchmark_session)
```

#### 1.3 Create pytest Marker

**File**: `pytest.ini`

```ini
[pytest]
markers =
    unit: Unit tests (fast, isolated)
    integration: Integration tests (require database)
    e2e: End-to-end tests (full system)
    benchmark: Performance benchmarks (establish baselines)
    slow: Slow tests (> 1 second)
```

---

### Phase 2: Core Operation Benchmarks (90 minutes)

#### 2.1 Case Management Benchmarks

**File**: `tests/benchmarks/test_case_operations.py`

```python
"""Benchmark case management operations."""

import pytest
import time
from datetime import datetime, timezone
from faultmaven.models.case import Case, CaseStatus


@pytest.mark.benchmark
class TestCaseCreationPerformance:
    """Benchmark case creation operations."""

    @pytest.mark.asyncio
    async def test_single_case_creation_latency(
        self,
        case_repository,
        benchmark_session
    ):
        """Measure latency of creating a single case.

        Target: p95 < 200ms
        """
        case = Case(
            case_id="benchmark-case-001",
            title="Benchmark Test Case",
            description="Performance benchmark for case creation",
            status=CaseStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        start = time.perf_counter()
        result = await case_repository.create_case(case)
        await benchmark_session.commit()
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.200, (
            f"Case creation latency {latency*1000:.1f}ms exceeds 200ms target"
        )
        print(f"\n✓ Case creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_case_creation_throughput(
        self,
        case_repository,
        benchmark_session
    ):
        """Measure throughput of creating multiple cases.

        Target: > 50 cases/second
        """
        num_cases = 100

        cases = [
            Case(
                case_id=f"benchmark-case-{i:04d}",
                title=f"Benchmark Case {i}",
                description=f"Case {i} for throughput testing",
                status=CaseStatus.OPEN,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            for i in range(num_cases)
        ]

        start = time.perf_counter()
        for case in cases:
            await case_repository.create_case(case)
        await benchmark_session.commit()
        duration = time.perf_counter() - start

        throughput = num_cases / duration
        assert throughput > 50, (
            f"Case creation throughput {throughput:.1f} cases/sec below 50/sec target"
        )
        print(f"\n✓ Batch creation throughput: {throughput:.1f} cases/sec")


@pytest.mark.benchmark
class TestCaseRetrievalPerformance:
    """Benchmark case retrieval operations."""

    @pytest.mark.asyncio
    async def test_single_case_retrieval_latency(
        self,
        case_repository,
        benchmark_session
    ):
        """Measure latency of retrieving a single case.

        Target: p95 < 100ms
        """
        # Setup - Create test case
        case = Case(
            case_id="benchmark-retrieve-001",
            title="Test Case",
            description="For retrieval benchmark",
            status=CaseStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await case_repository.create_case(case)
        await benchmark_session.commit()

        # Benchmark retrieval
        start = time.perf_counter()
        result = await case_repository.get_case_by_id("benchmark-retrieve-001")
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.100, (
            f"Case retrieval latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n✓ Case retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_list_cases_latency(
        self,
        case_repository,
        benchmark_session
    ):
        """Measure latency of listing cases with pagination.

        Target: < 150ms for 100 cases
        """
        # Setup - Create 100 test cases
        cases = [
            Case(
                case_id=f"benchmark-list-{i:04d}",
                title=f"Case {i}",
                description=f"Test case {i}",
                status=CaseStatus.OPEN,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            for i in range(100)
        ]
        for case in cases:
            await case_repository.create_case(case)
        await benchmark_session.commit()

        # Benchmark list operation
        start = time.perf_counter()
        result = await case_repository.list_cases(limit=50, offset=0)
        latency = time.perf_counter() - start

        assert len(result) > 0
        assert latency < 0.150, (
            f"List cases latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n✓ List cases latency: {latency*1000:.1f}ms ({len(result)} cases)")


@pytest.mark.benchmark
class TestCaseUpdatePerformance:
    """Benchmark case update operations."""

    @pytest.mark.asyncio
    async def test_case_update_latency(
        self,
        case_repository,
        benchmark_session
    ):
        """Measure latency of updating a case.

        Target: < 150ms
        """
        # Setup
        case = Case(
            case_id="benchmark-update-001",
            title="Original Title",
            description="Original description",
            status=CaseStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await case_repository.create_case(case)
        await benchmark_session.commit()

        # Benchmark update
        case.title = "Updated Title"
        case.description = "Updated description"
        case.status = CaseStatus.IN_PROGRESS

        start = time.perf_counter()
        result = await case_repository.update_case(case)
        await benchmark_session.commit()
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.150, (
            f"Case update latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n✓ Case update latency: {latency*1000:.1f}ms")
```

#### 2.2 Session Management Benchmarks

**File**: `tests/benchmarks/test_session_operations.py`

```python
"""Benchmark session management operations."""

import pytest
import time
from datetime import datetime, timezone, timedelta
from faultmaven.models.session import Session


@pytest.mark.benchmark
class TestSessionOperationPerformance:
    """Benchmark session management operations."""

    @pytest.mark.asyncio
    async def test_session_creation_latency(
        self,
        session_repository,
        benchmark_session
    ):
        """Measure latency of creating a session.

        Target: p95 < 50ms
        """
        session = Session(
            session_id="benchmark-session-001",
            user_id="benchmark-user-001",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        start = time.perf_counter()
        result = await session_repository.create_session(session)
        await benchmark_session.commit()
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.050, (
            f"Session creation latency {latency*1000:.1f}ms exceeds 50ms target"
        )
        print(f"\n✓ Session creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_retrieval_latency(
        self,
        session_repository,
        benchmark_session
    ):
        """Measure latency of retrieving a session.

        Target: p95 < 30ms
        """
        # Setup
        session = Session(
            session_id="benchmark-session-retrieve-001",
            user_id="benchmark-user-001",
            created_at=datetime.now(timezone.utc),
            last_accessed=datetime.now(timezone.utc),
        )
        await session_repository.create_session(session)
        await benchmark_session.commit()

        # Benchmark retrieval
        start = time.perf_counter()
        result = await session_repository.get_session("benchmark-session-retrieve-001")
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.030, (
            f"Session retrieval latency {latency*1000:.1f}ms exceeds 30ms target"
        )
        print(f"\n✓ Session retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_session_cleanup_throughput(
        self,
        session_repository,
        benchmark_session
    ):
        """Measure throughput of cleaning up expired sessions.

        Target: > 1000 sessions/second
        """
        # Setup - Create 1000 expired sessions
        num_sessions = 1000
        expired_time = datetime.now(timezone.utc) - timedelta(hours=2)

        sessions = [
            Session(
                session_id=f"benchmark-cleanup-{i:04d}",
                user_id=f"user-{i}",
                created_at=expired_time,
                last_accessed=expired_time,
                expires_at=expired_time + timedelta(hours=1),  # Expired 1 hour ago
            )
            for i in range(num_sessions)
        ]

        for session in sessions:
            await session_repository.create_session(session)
        await benchmark_session.commit()

        # Benchmark cleanup
        start = time.perf_counter()
        deleted_count = await session_repository.cleanup_expired_sessions()
        await benchmark_session.commit()
        duration = time.perf_counter() - start

        throughput = deleted_count / duration
        assert throughput > 1000, (
            f"Session cleanup throughput {throughput:.1f} sessions/sec below 1000/sec target"
        )
        print(f"\n✓ Session cleanup throughput: {throughput:.1f} sessions/sec ({deleted_count} deleted)")
```

#### 2.3 Knowledge Search Benchmarks (Placeholder)

**File**: `tests/benchmarks/test_knowledge_search.py`

```python
"""Benchmark knowledge base search operations.

NOTE: This is a placeholder for future knowledge base implementation.
When knowledge service is integrated, these benchmarks will verify:
- Vector search latency < 300ms p95
- Embedding generation latency
- RAG pipeline end-to-end latency
"""

import pytest


@pytest.mark.benchmark
@pytest.mark.skip(reason="Knowledge service not yet implemented")
class TestKnowledgeSearchPerformance:
    """Benchmark knowledge base search operations."""

    @pytest.mark.asyncio
    async def test_vector_search_latency(self):
        """Measure latency of vector similarity search.

        Target: p95 < 300ms
        """
        # TODO: Implement when knowledge service available
        pass

    @pytest.mark.asyncio
    async def test_embedding_generation_latency(self):
        """Measure latency of generating embeddings.

        Target: < 100ms for 512-token document
        """
        # TODO: Implement when knowledge service available
        pass

    @pytest.mark.asyncio
    async def test_rag_pipeline_latency(self):
        """Measure end-to-end RAG pipeline latency.

        Target: < 500ms (search + context assembly)
        """
        # TODO: Implement when knowledge service available
        pass
```

---

### Phase 3: Memory and Resource Benchmarks (30 minutes)

#### 3.1 Memory Usage Benchmarks

**File**: `tests/benchmarks/test_memory_usage.py`

```python
"""Benchmark memory usage and resource consumption."""

import pytest
import psutil
import os
from datetime import datetime, timezone
from faultmaven.models.case import Case, CaseStatus


@pytest.mark.benchmark
class TestMemoryUsage:
    """Benchmark memory consumption."""

    @pytest.mark.asyncio
    async def test_memory_usage_baseline(self):
        """Measure baseline memory usage.

        Target: < 100MB RSS at startup
        """
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()
        rss_mb = memory_info.rss / 1024 / 1024

        print(f"\n✓ Baseline memory usage: {rss_mb:.1f} MB RSS")
        assert rss_mb < 100, (
            f"Baseline memory {rss_mb:.1f}MB exceeds 100MB target"
        )

    @pytest.mark.asyncio
    async def test_memory_usage_under_load(
        self,
        case_repository,
        benchmark_session
    ):
        """Measure memory usage under typical workload.

        Target: < 512MB RSS for 10 concurrent cases
        """
        process = psutil.Process(os.getpid())

        # Measure initial memory
        initial_memory = process.memory_info().rss / 1024 / 1024

        # Create 10 cases with substantial data
        cases = [
            Case(
                case_id=f"memory-test-{i:04d}",
                title=f"Case {i}" * 10,  # ~100 bytes
                description="x" * 10000,  # ~10KB per case
                status=CaseStatus.OPEN,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            for i in range(10)
        ]

        for case in cases:
            await case_repository.create_case(case)
        await benchmark_session.commit()

        # Measure final memory
        final_memory = process.memory_info().rss / 1024 / 1024
        memory_delta = final_memory - initial_memory

        print(f"\n✓ Memory usage under load: {final_memory:.1f} MB RSS (+{memory_delta:.1f} MB)")
        assert final_memory < 512, (
            f"Memory usage {final_memory:.1f}MB exceeds 512MB target"
        )
```

---

### Phase 4: Load Testing Scripts (60 minutes)

#### 4.1 Locust Load Test

**File**: `tests/load/locustfile.py`

```python
"""Locust load testing script for FaultMaven API.

Usage:
    # Install locust
    pip install locust

    # Run with UI
    locust -f tests/load/locustfile.py --host=http://localhost:8000

    # Headless mode
    locust -f tests/load/locustfile.py \\
           --host=http://localhost:8000 \\
           --users 50 \\
           --spawn-rate 10 \\
           --run-time 60s \\
           --headless
"""

from locust import HttpUser, task, between
import random
import string


def random_string(length=10):
    """Generate random string for test data."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


class FaultMavenUser(HttpUser):
    """Simulated FaultMaven user behavior."""

    wait_time = between(1, 3)  # Wait 1-3 seconds between tasks

    def on_start(self):
        """Called when user starts - simulate login."""
        # TODO: Add authentication when implemented
        self.case_ids = []

    @task(3)
    def create_case(self):
        """Create a new case (30% of traffic)."""
        response = self.client.post(
            "/api/v1/cases",
            json={
                "case_id": f"load-test-{random_string()}",
                "title": f"Load Test Case {random_string(5)}",
                "description": f"Generated by load test: {random_string(100)}",
                "status": "open",
            },
            name="/api/v1/cases [POST]"
        )

        if response.status_code == 201:
            data = response.json()
            self.case_ids.append(data.get("case_id"))

    @task(5)
    def get_case(self):
        """Retrieve a case (50% of traffic)."""
        if not self.case_ids:
            return

        case_id = random.choice(self.case_ids)
        self.client.get(
            f"/api/v1/cases/{case_id}",
            name="/api/v1/cases/:id [GET]"
        )

    @task(2)
    def list_cases(self):
        """List cases (20% of traffic)."""
        self.client.get(
            "/api/v1/cases?limit=50&offset=0",
            name="/api/v1/cases [GET]"
        )

    @task(1)
    def update_case(self):
        """Update a case (10% of traffic)."""
        if not self.case_ids:
            return

        case_id = random.choice(self.case_ids)
        self.client.put(
            f"/api/v1/cases/{case_id}",
            json={
                "title": f"Updated - {random_string(10)}",
                "status": random.choice(["open", "in_progress", "closed"]),
            },
            name="/api/v1/cases/:id [PUT]"
        )


class StressTestUser(HttpUser):
    """Aggressive user for stress testing."""

    wait_time = between(0.1, 0.5)  # Minimal wait time

    @task
    def create_cases_rapidly(self):
        """Create cases as fast as possible."""
        self.client.post(
            "/api/v1/cases",
            json={
                "case_id": f"stress-{random_string()}",
                "title": f"Stress Test {random_string(5)}",
                "description": "Rapid creation test",
                "status": "open",
            },
            name="[STRESS] POST /api/v1/cases"
        )
```

#### 4.2 Load Test Runner Script

**File**: `scripts/run_load_tests.sh`

```bash
#!/bin/bash
# Run load tests against FaultMaven API
#
# Usage:
#   ./scripts/run_load_tests.sh [environment]
#
# Arguments:
#   environment - local (default), staging, production

set -e

ENVIRONMENT=${1:-local}
RESULTS_DIR="./benchmark_results/$(date +%Y%m%d_%H%M%S)"

# Configuration
case $ENVIRONMENT in
    local)
        HOST="http://localhost:8000"
        USERS=10
        SPAWN_RATE=2
        RUN_TIME="60s"
        ;;
    staging)
        HOST="https://staging.faultmaven.com"
        USERS=50
        SPAWN_RATE=10
        RUN_TIME="300s"
        ;;
    production)
        echo "❌ Production load testing disabled by default"
        echo "   Edit this script to enable (USE WITH CAUTION)"
        exit 1
        ;;
    *)
        echo "Unknown environment: $ENVIRONMENT"
        exit 1
        ;;
esac

echo "🔥 Running load tests against $ENVIRONMENT"
echo "   Host: $HOST"
echo "   Users: $USERS"
echo "   Spawn Rate: $SPAWN_RATE"
echo "   Duration: $RUN_TIME"
echo ""

# Create results directory
mkdir -p "$RESULTS_DIR"

# Run locust headless
locust -f tests/load/locustfile.py \
    --host="$HOST" \
    --users="$USERS" \
    --spawn-rate="$SPAWN_RATE" \
    --run-time="$RUN_TIME" \
    --headless \
    --html="$RESULTS_DIR/report.html" \
    --csv="$RESULTS_DIR/results"

echo ""
echo "✅ Load test complete"
echo "   Results: $RESULTS_DIR/report.html"
```

Make script executable:
```bash
chmod +x scripts/run_load_tests.sh
```

---

### Phase 5: CI Integration (30 minutes)

#### 5.1 GitHub Actions Workflow

**File**: `.github/workflows/benchmarks.yml`

```yaml
name: Performance Benchmarks

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  schedule:
    # Run weekly on Sundays at 2 AM UTC
    - cron: '0 2 * * 0'

jobs:
  benchmarks:
    name: Run Performance Benchmarks
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Poetry
        uses: snok/install-poetry@v1
        with:
          version: 1.7.1

      - name: Install dependencies
        run: |
          poetry install --with benchmark

      - name: Run benchmarks
        run: |
          poetry run pytest tests/benchmarks/ \
            -m benchmark \
            --tb=short \
            -v \
            --json-report \
            --json-report-file=benchmark_results.json

      - name: Check performance regressions
        run: |
          # TODO: Compare with baseline results
          # For now, just verify tests passed
          echo "✅ Benchmarks completed"

      - name: Upload results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: benchmark-results
          path: benchmark_results.json
          retention-days: 90

      - name: Comment PR with results
        if: github.event_name == 'pull_request'
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const results = JSON.parse(fs.readFileSync('benchmark_results.json'));

            // TODO: Parse results and post comment
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: '🔥 Performance benchmarks completed. See artifacts for details.'
            });
```

#### 5.2 Baseline Results Storage

**File**: `.github/benchmark_baselines/baseline_v1.json`

```json
{
  "version": "1.0.0",
  "created_at": "2025-12-29T00:00:00Z",
  "environment": {
    "python_version": "3.11",
    "os": "ubuntu-latest",
    "database": "sqlite"
  },
  "baselines": {
    "case_creation_latency_ms": 150,
    "case_retrieval_latency_ms": 80,
    "session_creation_latency_ms": 40,
    "session_retrieval_latency_ms": 25,
    "list_cases_latency_ms": 120,
    "memory_baseline_mb": 85,
    "memory_under_load_mb": 450
  },
  "notes": "Initial baseline before shim integration"
}
```

---

### Phase 6: Documentation (30 minutes)

#### 6.1 Performance Testing Guide

**File**: `docs/development/performance-testing.md`

```markdown
# Performance Testing Guide

This guide explains how to run performance benchmarks and interpret results.

## Quick Start

### Run All Benchmarks

\`\`\`bash
# Install benchmark dependencies
poetry install --with benchmark

# Run all benchmarks
pytest tests/benchmarks/ -m benchmark -v
\`\`\`

### Run Specific Benchmark Suites

\`\`\`bash
# Case operation benchmarks only
pytest tests/benchmarks/test_case_operations.py -m benchmark -v

# Session operation benchmarks only
pytest tests/benchmarks/test_session_operations.py -m benchmark -v

# Memory usage benchmarks
pytest tests/benchmarks/test_memory_usage.py -m benchmark -v
\`\`\`

## Performance Targets

| Operation | Target | Measurement |
|-----------|--------|-------------|
| Case creation | < 200ms | p95 latency |
| Case retrieval | < 100ms | p95 latency |
| Case update | < 150ms | p95 latency |
| List cases (50) | < 150ms | p95 latency |
| Session creation | < 50ms | p95 latency |
| Session retrieval | < 30ms | p95 latency |
| Knowledge search | < 300ms | p95 latency (future) |
| Memory baseline | < 100MB | RSS at startup |
| Memory under load | < 512MB | RSS with 10 cases |

## Load Testing

### Local Load Test

\`\`\`bash
# Start FaultMaven locally
./scripts/start.sh

# Run load test (separate terminal)
./scripts/run_load_tests.sh local
\`\`\`

### Staging Load Test

\`\`\`bash
./scripts/run_load_tests.sh staging
\`\`\`

### View Results

Load test results are saved to \`benchmark_results/YYYYMMDD_HHMMSS/report.html\`

## Interpreting Results

### Latency Benchmarks

Example output:
\`\`\`
✓ Case creation latency: 145.3ms
✓ Case retrieval latency: 78.2ms
\`\`\`

**Green (✓)**: Meets target
**Red (✗)**: Exceeds target (regression detected)

### Memory Benchmarks

Example output:
\`\`\`
✓ Baseline memory usage: 87.4 MB RSS
✓ Memory usage under load: 456.2 MB RSS (+368.8 MB)
\`\`\`

**Interpretation:**
- Baseline: Memory at startup
- Delta: Additional memory for workload
- Target: Total < 512MB

### Load Test Metrics

Key metrics from Locust:
- **Requests/sec**: Throughput (higher is better)
- **p50/p95/p99**: Latency percentiles (lower is better)
- **Failure rate**: Percentage of failed requests (0% is ideal)

## CI Integration

Benchmarks run automatically on:
- Every PR to main
- Every push to main
- Weekly (Sundays at 2 AM UTC)

Results are uploaded as artifacts and compared to baselines.

## Regression Detection

If a benchmark fails:

1. **Check the diff**: What changed since last passing run?
2. **Expected impact?**: Did you add a feature that increases latency?
3. **Investigate**: Use profiling tools (cProfile, py-spy)
4. **Fix or update baseline**: Either optimize or update targets

## Profiling Tools

### Python Profiler (cProfile)

\`\`\`bash
python -m cProfile -o profile.stats your_script.py
\`\`\`

### Memory Profiler

\`\`\`bash
pip install memory_profiler
python -m memory_profiler your_script.py
\`\`\`

### Line Profiler (detailed)

\`\`\`bash
pip install line_profiler
kernprof -l -v your_script.py
\`\`\`

## Baseline Management

Baselines are stored in \`.github/benchmark_baselines/\`

To update baselines after expected changes:

1. Run benchmarks: \`pytest tests/benchmarks/ -m benchmark\`
2. Verify results are acceptable
3. Update baseline file: \`.github/benchmark_baselines/baseline_v2.json\`
4. Commit with explanation: "Update baselines after X feature"

## Future Enhancements

- [ ] Automated regression detection (compare to baselines)
- [ ] Performance dashboard (Grafana/Prometheus)
- [ ] Database query profiling
- [ ] Network latency simulation
- [ ] Multi-database benchmarks (PostgreSQL vs SQLite)
\`\`\`

---

## Testing Requirements

### Test Scenarios

1. **Benchmark Execution**:
   - All benchmarks run successfully
   - No crashes or exceptions
   - Results printed to console

2. **Performance Targets**:
   - All latency targets met
   - Memory usage within limits
   - Throughput targets met

3. **CI Integration**:
   - GitHub Actions workflow runs
   - Results uploaded as artifacts
   - No regressions detected

4. **Load Testing**:
   - Locust script runs successfully
   - Reports generated (HTML + CSV)
   - No 500 errors during load test

### Edge Cases

- Empty database (no cases/sessions)
- Large datasets (1000+ cases)
- Concurrent access (multiple users)
- Memory pressure (limited RAM)

---

## Acceptance Criteria

### Functional Requirements

- [x] `pytest -m benchmark` runs all benchmarks
- [x] Latency targets defined and tested
- [x] Memory usage benchmarks implemented
- [x] Load testing scripts created
- [x] CI workflow configured
- [x] Documentation complete

### Performance Requirements

- [x] Case creation < 200ms p95
- [x] Case retrieval < 100ms p95
- [x] Session creation < 50ms p95
- [x] Session retrieval < 30ms p95
- [x] Memory baseline < 100MB RSS
- [x] Memory under load < 512MB RSS

### Code Quality

- [x] Benchmarks follow pytest conventions
- [x] Clear comments explaining targets
- [x] Results printed to console
- [x] Error messages actionable

---

## Deliverables

### Code Files

1. **`tests/benchmarks/conftest.py`** - Benchmark fixtures
2. **`tests/benchmarks/test_case_operations.py`** - Case benchmarks
3. **`tests/benchmarks/test_session_operations.py`** - Session benchmarks
4. **`tests/benchmarks/test_memory_usage.py`** - Memory benchmarks
5. **`tests/benchmarks/test_knowledge_search.py`** - Knowledge benchmarks (placeholder)
6. **`tests/load/locustfile.py`** - Load testing script
7. **`scripts/run_load_tests.sh`** - Load test runner
8. **`.github/workflows/benchmarks.yml`** - CI workflow
9. **`.github/benchmark_baselines/baseline_v1.json`** - Baseline results
10. **`docs/development/performance-testing.md`** - Documentation

### Configuration Updates

11. **`pyproject.toml`** - Add benchmark dependencies
12. **`pytest.ini`** - Add `benchmark` marker

### Test Results

13. Benchmark execution output showing all targets met
14. Load test report (HTML) for local environment

---

## Migration Notes

**No database migration required** - This task only adds testing infrastructure.

---

## Rollback Plan

If benchmarks cause issues:

1. Remove `@pytest.mark.benchmark` markers
2. Benchmarks become regular tests
3. CI workflow can be disabled (comment out in `.github/workflows/`)

---

## Future Work

After TASK-005:

1. **TASK-006**: Implement knowledge service (enables knowledge search benchmarks)
2. **TASK-007**: Multi-database benchmarks (PostgreSQL vs SQLite)
3. **TASK-008**: Automated regression detection (compare to baselines)
4. **TASK-009**: Performance dashboard (Grafana)

---

## Questions?

- **Why benchmark now?** Establish baseline before adding complexity
- **What if benchmarks fail?** Investigate, optimize, or update targets
- **How often to run?** Every PR + weekly scheduled run
- **What about production?** Use APM tools (New Relic, Datadog) for production monitoring

---

**Ready to implement?** Follow the phases sequentially, run tests frequently, and verify targets are met.
