"""Benchmark evidence artifact management operations.

Establishes performance baselines for evidence artifact CRUD operations.

Performance Targets (p95 latency):
- Evidence creation: < 150ms
- Evidence retrieval: < 100ms
- List evidence (20 items): < 100ms
- Evidence deletion: < 100ms

Run with:
    pytest tests/benchmarks/test_evidence_artifact_operations.py -m benchmark -v
"""

import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    DatabaseEvidenceArtifactRepository,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InvestigationStrategy,
)
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    StorageBackend,
)
from tests.utils import generate_evidence_id

from .conftest import generate_case_id


def create_sample_evidence(
    case_id: str,
    evidence_type: EvidenceArtifactType = EvidenceArtifactType.SCREENSHOT,
) -> EvidenceArtifact:
    """Create a sample evidence artifact for benchmarking."""
    return EvidenceArtifact(
        evidence_id=generate_evidence_id(),
        case_id=case_id,
        user_id="benchmark-user-001",
        organization_id="benchmark-org-001",
        original_filename=f"benchmark_file_{uuid4().hex[:8]}.png",
        stored_filename=f"{generate_evidence_id()}.png",
        file_path=f"/evidence/2025/12/{uuid4().hex[:8]}.png",
        evidence_type=evidence_type,
        mime_type="image/png",
        file_size=1024000,  # 1MB
        storage_backend=StorageBackend.LOCAL_FILESYSTEM,
    )


@pytest.fixture
async def evidence_repository(benchmark_session) -> DatabaseEvidenceArtifactRepository:
    """Create evidence artifact repository for benchmarks."""
    return DatabaseEvidenceArtifactRepository(benchmark_session)


@pytest.fixture
async def benchmark_case(case_repository: DatabaseCaseRepository) -> Case:
    """Create a case for benchmark evidence operations."""
    case = Case(
        case_id=generate_case_id(),
        user_id="benchmark-user-001",
        organization_id="benchmark-org-001",
        title="Benchmark Evidence Case",
        description="Case for benchmarking evidence operations",
        status=CaseStatus.INQUIRY,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )
    return await case_repository.save(case)


@pytest.mark.benchmark
class TestEvidenceCreationPerformance:
    """Benchmark evidence creation operations."""

    @pytest.mark.asyncio
    async def test_single_evidence_creation_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of creating a single evidence artifact.

        Target: p95 < 150ms
        """
        evidence = create_sample_evidence(benchmark_case.case_id)

        start = time.perf_counter()
        result = await evidence_repository.create_evidence(evidence)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.150
        ), f"Evidence creation latency {latency*1000:.1f}ms exceeds 150ms target"
        print(f"\n  Evidence creation latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_batch_evidence_creation_throughput(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure throughput of creating multiple evidence artifacts.

        Target: > 30 evidence/second
        """
        num_evidence = 50

        evidence_items = [
            create_sample_evidence(benchmark_case.case_id) for _ in range(num_evidence)
        ]

        start = time.perf_counter()
        for evidence in evidence_items:
            await evidence_repository.create_evidence(evidence)
        duration = time.perf_counter() - start

        throughput = num_evidence / duration
        assert (
            throughput > 30
        ), f"Evidence creation throughput {throughput:.1f}/sec below 30/sec target"
        print(
            f"\n  Batch creation throughput: {throughput:.1f} evidence/sec "
            f"({num_evidence} items in {duration:.2f}s)"
        )


@pytest.mark.benchmark
class TestEvidenceRetrievalPerformance:
    """Benchmark evidence retrieval operations."""

    @pytest.mark.asyncio
    async def test_single_evidence_retrieval_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of retrieving a single evidence artifact.

        Target: p95 < 100ms
        """
        # Setup - Create test evidence
        evidence = create_sample_evidence(benchmark_case.case_id)
        await evidence_repository.create_evidence(evidence)

        # Benchmark retrieval
        start = time.perf_counter()
        result = await evidence_repository.get_evidence(evidence.evidence_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.100
        ), f"Evidence retrieval latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Evidence retrieval latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing evidence for a case.

        Target: < 100ms for 20 evidence items
        """
        # Setup - Create 20 evidence records
        for i in range(20):
            evidence = create_sample_evidence(benchmark_case.case_id)
            await evidence_repository.create_evidence(evidence)

        # Benchmark list operation
        start = time.perf_counter()
        result, total = await evidence_repository.list_evidence_by_case(
            case_id=benchmark_case.case_id,
            limit=50,
        )
        latency = time.perf_counter() - start

        assert len(result) == 20
        assert total == 20
        assert (
            latency < 0.100
        ), f"List evidence latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  List evidence latency: {latency*1000:.1f}ms ({len(result)} items)")

    @pytest.mark.asyncio
    async def test_list_evidence_with_type_filter_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of listing evidence with type filter.

        Target: < 100ms for filtered query
        """
        # Setup - Create mixed evidence types
        for i in range(10):
            evidence = create_sample_evidence(
                benchmark_case.case_id,
                evidence_type=EvidenceArtifactType.SCREENSHOT,
            )
            await evidence_repository.create_evidence(evidence)

        for i in range(10):
            evidence = create_sample_evidence(
                benchmark_case.case_id,
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )
            await evidence_repository.create_evidence(evidence)

        # Benchmark filtered list
        start = time.perf_counter()
        result, total = await evidence_repository.list_evidence_by_case(
            case_id=benchmark_case.case_id,
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            limit=50,
        )
        latency = time.perf_counter() - start

        assert len(result) == 10
        assert total == 10
        assert (
            latency < 0.100
        ), f"Filtered list latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Filtered list latency: {latency*1000:.1f}ms ({len(result)} items)")


@pytest.mark.benchmark
class TestEvidenceUpdatePerformance:
    """Benchmark evidence update operations."""

    @pytest.mark.asyncio
    async def test_evidence_update_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of updating an evidence artifact.

        Target: < 100ms
        """
        # Setup - Create test evidence
        evidence = create_sample_evidence(benchmark_case.case_id)
        await evidence_repository.create_evidence(evidence)

        # Modify evidence
        evidence.description = "Updated description for benchmark"
        evidence.is_primary = True
        evidence.metadata = {"updated": True, "benchmark": True}

        # Benchmark update
        start = time.perf_counter()
        result = await evidence_repository.update_evidence(evidence)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.100
        ), f"Evidence update latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Evidence update latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestEvidenceDeletePerformance:
    """Benchmark evidence deletion operations."""

    @pytest.mark.asyncio
    async def test_evidence_delete_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of deleting an evidence artifact.

        Target: < 100ms
        """
        # Setup - Create test evidence
        evidence = create_sample_evidence(benchmark_case.case_id)
        await evidence_repository.create_evidence(evidence)

        # Benchmark delete
        start = time.perf_counter()
        result = await evidence_repository.delete_evidence(evidence.evidence_id)
        latency = time.perf_counter() - start

        assert result is True
        assert (
            latency < 0.100
        ), f"Evidence delete latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Evidence delete latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestPrimaryEvidencePerformance:
    """Benchmark primary evidence operations."""

    @pytest.mark.asyncio
    async def test_set_primary_evidence_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of setting primary evidence.

        Target: < 100ms
        """
        # Setup - Create multiple evidence items
        evidence_items = []
        for i in range(5):
            evidence = create_sample_evidence(benchmark_case.case_id)
            await evidence_repository.create_evidence(evidence)
            evidence_items.append(evidence)

        # Benchmark set primary
        start = time.perf_counter()
        result = await evidence_repository.set_primary_evidence(
            benchmark_case.case_id,
            evidence_items[2].evidence_id,
        )
        latency = time.perf_counter() - start

        assert result is True
        assert (
            latency < 0.100
        ), f"Set primary latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Set primary evidence latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_get_primary_evidence_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of getting primary evidence.

        Target: < 100ms
        """
        # Setup - Create evidence and set as primary
        evidence = create_sample_evidence(benchmark_case.case_id)
        await evidence_repository.create_evidence(evidence)
        await evidence_repository.set_primary_evidence(
            benchmark_case.case_id,
            evidence.evidence_id,
        )

        # Benchmark get primary
        start = time.perf_counter()
        result = await evidence_repository.get_primary_evidence(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert result is not None
        assert (
            latency < 0.100
        ), f"Get primary latency {latency*1000:.1f}ms exceeds 100ms target"
        print(f"\n  Get primary evidence latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestEvidenceCountPerformance:
    """Benchmark evidence count operations."""

    @pytest.mark.asyncio
    async def test_count_evidence_latency(
        self,
        evidence_repository: DatabaseEvidenceArtifactRepository,
        benchmark_case: Case,
        benchmark_session,
    ):
        """Measure latency of counting evidence for a case.

        Target: < 50ms
        """
        # Setup - Create 50 evidence items
        for i in range(50):
            evidence = create_sample_evidence(benchmark_case.case_id)
            await evidence_repository.create_evidence(evidence)

        # Benchmark count
        start = time.perf_counter()
        count = await evidence_repository.count_by_case(benchmark_case.case_id)
        latency = time.perf_counter() - start

        assert count == 50
        assert (
            latency < 0.050
        ), f"Count latency {latency*1000:.1f}ms exceeds 50ms target"
        print(f"\n  Count evidence latency: {latency*1000:.1f}ms ({count} items)")
