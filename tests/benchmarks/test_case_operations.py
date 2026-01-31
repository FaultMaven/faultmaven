"""Benchmark case management operations.

Establishes performance baselines for case CRUD operations.

Performance Targets (p95 latency):
- Case creation: < 200ms
- Case retrieval: < 100ms
- Case update: < 150ms
- List cases (50): < 150ms

Run with:
    pytest tests/benchmarks/test_case_operations.py -m benchmark -v
"""

import time
from datetime import datetime, timezone

import pytest

from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationStrategy,
)

from .conftest import generate_case_id


@pytest.mark.benchmark
class TestCaseCreationPerformance:
    """Benchmark case creation operations."""

    @pytest.mark.asyncio
    async def test_single_case_creation_latency(
        self,
        case_repository: DatabaseCaseRepository,
        benchmark_session,
    ):
        """Measure latency of creating a single case.

        Target: p95 < 200ms
        """
        case = Case(
            case_id=generate_case_id(),
            user_id="benchmark-user-001",
            organization_id="benchmark-org-001",
            title="Benchmark Test Case",
            description="Performance benchmark for case creation",
            status=CaseStatus.INQUIRY,
            investigation_strategy=InvestigationStrategy.POST_MORTEM,
        )

        start = time.perf_counter()
        result = await case_repository.save(case)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.200
        ), f"Case creation latency {latency*1000:.1f}ms exceeds 200ms target"
        print(f"\n  Case creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_case_creation_throughput(
        self,
        case_repository: DatabaseCaseRepository,
        benchmark_session,
    ):
        """Measure throughput of creating multiple cases.

        Target: > 50 cases/second
        """
        num_cases = 100

        cases = [
            Case(
                case_id=generate_case_id(),
                user_id="benchmark-user-001",
                organization_id="benchmark-org-001",
                title=f"Benchmark Case {i}",
                description=f"Case {i} for throughput testing",
                status=CaseStatus.INQUIRY,
                investigation_strategy=InvestigationStrategy.POST_MORTEM,
            )
            for i in range(num_cases)
        ]

        start = time.perf_counter()
        for case in cases:
            await case_repository.save(case)
        duration = time.perf_counter() - start

        throughput = num_cases / duration
        assert (
            throughput > 50
        ), f"Case creation throughput {throughput:.1f} cases/sec below 50/sec target"
        print(
            f"\n  Batch creation throughput: {throughput:.1f} cases/sec ({num_cases} cases in {duration:.2f}s)"
        )


@pytest.mark.benchmark
class TestCaseRetrievalPerformance:
    """Benchmark case retrieval operations."""

    @pytest.mark.asyncio
    async def test_single_case_retrieval_latency(
        self,
        case_repository: DatabaseCaseRepository,
        benchmark_session,
    ):
        """Measure latency of retrieving a single case.

        Target: p95 < 100ms
        """
        # Setup - Create test case
        case_id = generate_case_id()
        case = Case(
            case_id=case_id,
            user_id="benchmark-user-001",
            organization_id="benchmark-org-001",
            title="Test Case for Retrieval",
            description="For retrieval benchmark",
            status=CaseStatus.INQUIRY,
            investigation_strategy=InvestigationStrategy.POST_MORTEM,
        )
        await case_repository.save(case)

        # Benchmark retrieval
        start = time.perf_counter()
        result = await case_repository.get(case_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.100
        ), f"Case retrieval latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Case retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_list_cases_latency(
        self,
        case_repository: DatabaseCaseRepository,
        benchmark_session,
    ):
        """Measure latency of listing cases with pagination.

        Target: < 150ms for 50 cases
        """
        # Setup - Create 100 test cases
        for i in range(100):
            case = Case(
                case_id=generate_case_id(),
                user_id="benchmark-user-001",
                organization_id="benchmark-org-001",
                title=f"List Test Case {i}",
                description=f"Test case {i}",
                status=CaseStatus.INQUIRY,
                investigation_strategy=InvestigationStrategy.POST_MORTEM,
            )
            await case_repository.save(case)

        # Benchmark list operation
        start = time.perf_counter()
        result, total = await case_repository.list(
            user_id="benchmark-user-001",
            limit=50,
            offset=0,
        )
        latency = time.perf_counter() - start

        assert len(result) > 0
        assert (
            latency < 0.150
        ), f"List cases latency {latency*1000:.1f}ms exceeds 150ms target"
        print(
            f"\n  List cases latency: {latency*1000:.1f}ms ({len(result)} cases returned)"
        )


@pytest.mark.benchmark
class TestCaseUpdatePerformance:
    """Benchmark case update operations."""

    @pytest.mark.asyncio
    async def test_case_update_latency(
        self,
        case_repository: DatabaseCaseRepository,
        benchmark_session,
    ):
        """Measure latency of updating a case.

        Target: < 150ms
        """
        # Setup - Create test case
        case_id = generate_case_id()
        case = Case(
            case_id=case_id,
            user_id="benchmark-user-001",
            organization_id="benchmark-org-001",
            title="Original Title",
            description="Original description",
            status=CaseStatus.INQUIRY,
            investigation_strategy=InvestigationStrategy.POST_MORTEM,
        )
        await case_repository.save(case)

        # Modify case
        case.title = "Updated Title"
        case.description = "Updated description"
        case.updated_at = datetime.now(timezone.utc)

        # Benchmark update
        start = time.perf_counter()
        result = await case_repository.save(case)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.150
        ), f"Case update latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Case update latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestCaseSearchPerformance:
    """Benchmark case search operations."""

    @pytest.mark.asyncio
    async def test_search_cases_latency(
        self,
        case_repository: DatabaseCaseRepository,
        benchmark_session,
    ):
        """Measure latency of searching cases.

        Target: < 200ms for text search
        """
        # Setup - Create searchable cases
        search_terms = ["database", "network", "memory", "timeout", "connection"]
        for i, term in enumerate(search_terms):
            for j in range(10):
                case = Case(
                    case_id=generate_case_id(),
                    user_id="benchmark-user-001",
                    organization_id="benchmark-org-001",
                    title=f"{term.capitalize()} Issue {j}",
                    description=f"A {term} related problem requiring investigation",
                    status=CaseStatus.INQUIRY,
                    investigation_strategy=InvestigationStrategy.POST_MORTEM,
                )
                await case_repository.save(case)

        # Benchmark search
        start = time.perf_counter()
        result, total = await case_repository.search(
            query="database",
            user_id="benchmark-user-001",
            limit=20,
        )
        latency = time.perf_counter() - start

        assert (
            latency < 0.200
        ), f"Search latency {latency*1000:.1f}ms exceeds 200ms target"
        print(f"\n  Search latency: {latency*1000:.1f}ms ({len(result)} results)")
