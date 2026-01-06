"""Benchmark evidence artifact service operations (TASK-013).

Establishes performance baselines for API evidence artifact service layer
operations including file storage I/O.

Performance Targets (p95 latency):
- Upload evidence (1MB file): < 500ms
- Upload evidence (10MB file): < 2000ms
- Get evidence metadata: < 100ms
- Download evidence (1MB file): < 400ms
- Delete evidence: < 200ms
- List evidence (50 artifacts): < 300ms
- Set primary evidence: < 150ms
- Get statistics (100 artifacts): < 400ms

File Storage Benchmarks:
- Store file (1MB): < 300ms
- Retrieve file (1MB): < 200ms
- Delete file: < 100ms

Run with:
    pytest tests/benchmarks/test_evidence_artifact_service_operations.py -m benchmark -v
"""

import os
import tempfile
import time
import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    DatabaseEvidenceArtifactRepository,
)
from faultmaven.services.file_storage_service import FileStorageService
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
from faultmaven.models.case import Case, CaseStatus, InvestigationStrategy
from faultmaven.modules.evidence.domain.models import EvidenceArtifactType


# ============================================================
# Helper Functions
# ============================================================


def generate_case_id() -> str:
    """Generate a valid case ID."""
    return f"case_{uuid4().hex[:12]}"


def generate_file_data(size_bytes: int) -> bytes:
    """Generate random file data of specified size."""
    return os.urandom(size_bytes)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    """Create in-memory SQLite engine for benchmarks."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def benchmark_session(async_engine) -> AsyncSession:
    """Create async session for benchmarks."""
    session_factory = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
def temp_storage_dir():
    """Create temporary directory for file storage benchmarks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
async def case_repository(benchmark_session) -> DatabaseCaseRepository:
    """Create case repository for benchmarks."""
    return DatabaseCaseRepository(benchmark_session)


@pytest.fixture
async def evidence_repository(benchmark_session) -> DatabaseEvidenceArtifactRepository:
    """Create evidence artifact repository for benchmarks."""
    return DatabaseEvidenceArtifactRepository(benchmark_session)


@pytest.fixture
def file_storage_service(temp_storage_dir) -> FileStorageService:
    """Create file storage service for benchmarks."""
    return FileStorageService(
        storage_root=temp_storage_dir,
        max_file_size_bytes=100 * 1024 * 1024,  # 100MB for benchmarks
        allowed_mime_types=[],
    )


@pytest.fixture
async def evidence_service(
    evidence_repository,
    case_repository,
    file_storage_service,
) -> APIEvidenceArtifactService:
    """Create evidence artifact service for benchmarks."""
    return APIEvidenceArtifactService(
        evidence_repo=evidence_repository,
        case_repo=case_repository,
        file_storage=file_storage_service,
    )


@pytest.fixture
async def benchmark_case(case_repository) -> Case:
    """Create a case for benchmark operations."""
    case = Case(
        case_id=generate_case_id(),
        user_id="benchmark-user-001",
        organization_id="benchmark-org-001",
        title="Benchmark Evidence Case",
        description="Case for benchmarking evidence service operations",
        status=CaseStatus.CONSULTING,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )
    return await case_repository.save(case)


@pytest.fixture
def file_1mb() -> bytes:
    """Generate 1MB file data."""
    return generate_file_data(1024 * 1024)


@pytest.fixture
def file_10mb() -> bytes:
    """Generate 10MB file data."""
    return generate_file_data(10 * 1024 * 1024)


# ============================================================
# File Storage Service Benchmarks
# ============================================================


@pytest.mark.benchmark
class TestFileStoragePerformance:
    """Benchmark file storage service operations."""

    @pytest.mark.asyncio
    async def test_store_file_1mb_latency(
        self, file_storage_service: FileStorageService, file_1mb: bytes
    ):
        """Measure latency of storing 1MB file.

        Target: p95 < 300ms
        """
        start = time.perf_counter()
        result = await file_storage_service.store_file(
            file_data=file_1mb,
            original_filename="benchmark_1mb.bin",
            organization_id="bench-org",
            case_id="bench-case",
            mime_type="application/octet-stream",
        )
        latency = time.perf_counter() - start

        assert result is not None
        assert result["file_size"] == len(file_1mb)
        assert latency < 0.300, (
            f"Store 1MB latency {latency*1000:.1f}ms exceeds 300ms target"
        )
        print(f"\n  Store 1MB file latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_retrieve_file_1mb_latency(
        self, file_storage_service: FileStorageService, file_1mb: bytes
    ):
        """Measure latency of retrieving 1MB file.

        Target: p95 < 200ms
        """
        # Setup - store file first
        store_result = await file_storage_service.store_file(
            file_data=file_1mb,
            original_filename="benchmark_retrieve.bin",
            organization_id="bench-org",
            case_id="bench-case",
            mime_type="application/octet-stream",
        )

        # Benchmark retrieval
        start = time.perf_counter()
        data = await file_storage_service.retrieve_file(store_result["file_path"])
        latency = time.perf_counter() - start

        assert len(data) == len(file_1mb)
        assert latency < 0.200, (
            f"Retrieve 1MB latency {latency*1000:.1f}ms exceeds 200ms target"
        )
        print(f"\n  Retrieve 1MB file latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_delete_file_latency(
        self, file_storage_service: FileStorageService, file_1mb: bytes
    ):
        """Measure latency of deleting file.

        Target: p95 < 100ms
        """
        # Setup - store file first
        store_result = await file_storage_service.store_file(
            file_data=file_1mb,
            original_filename="benchmark_delete.bin",
            organization_id="bench-org",
            case_id="bench-case",
            mime_type="application/octet-stream",
        )

        # Benchmark deletion
        start = time.perf_counter()
        deleted = await file_storage_service.delete_file(store_result["file_path"])
        latency = time.perf_counter() - start

        assert deleted is True
        assert latency < 0.100, (
            f"Delete file latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Delete file latency: {latency*1000:.1f}ms")


# ============================================================
# Evidence Artifact Service Benchmarks
# ============================================================


@pytest.mark.benchmark
class TestEvidenceUploadPerformance:
    """Benchmark evidence upload operations."""

    @pytest.mark.asyncio
    async def test_upload_evidence_1mb_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
        file_1mb: bytes,
    ):
        """Measure latency of uploading 1MB evidence.

        Target: p95 < 500ms
        """
        start = time.perf_counter()
        result = await evidence_service.upload_evidence(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
            user_id="benchmark-user",
            file_data=file_1mb,
            original_filename="benchmark_1mb.bin",
            mime_type="application/octet-stream",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            description="Benchmark 1MB upload",
        )
        latency = time.perf_counter() - start

        assert result is not None
        assert result.file_size == len(file_1mb)
        assert latency < 0.500, (
            f"Upload 1MB latency {latency*1000:.1f}ms exceeds 500ms target"
        )
        print(f"\n  Upload 1MB evidence latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_upload_evidence_10mb_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
        file_10mb: bytes,
    ):
        """Measure latency of uploading 10MB evidence.

        Target: p95 < 2000ms
        """
        start = time.perf_counter()
        result = await evidence_service.upload_evidence(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
            user_id="benchmark-user",
            file_data=file_10mb,
            original_filename="benchmark_10mb.bin",
            mime_type="application/octet-stream",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            description="Benchmark 10MB upload",
        )
        latency = time.perf_counter() - start

        assert result is not None
        assert result.file_size == len(file_10mb)
        assert latency < 2.0, (
            f"Upload 10MB latency {latency*1000:.1f}ms exceeds 2000ms target"
        )
        print(f"\n  Upload 10MB evidence latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestEvidenceRetrievalPerformance:
    """Benchmark evidence retrieval operations."""

    @pytest.mark.asyncio
    async def test_get_evidence_metadata_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
        file_1mb: bytes,
    ):
        """Measure latency of getting evidence metadata.

        Target: p95 < 100ms
        """
        # Setup - upload evidence
        uploaded = await evidence_service.upload_evidence(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
            user_id="benchmark-user",
            file_data=file_1mb,
            original_filename="benchmark.bin",
            mime_type="application/octet-stream",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        # Benchmark metadata retrieval
        start = time.perf_counter()
        result = await evidence_service.get_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=benchmark_case.organization_id,
        )
        latency = time.perf_counter() - start

        assert result is not None
        assert latency < 0.100, (
            f"Get metadata latency {latency*1000:.1f}ms exceeds 100ms target"
        )
        print(f"\n  Get evidence metadata latency: {latency*1000:.1f}ms")

    @pytest.mark.asyncio
    async def test_download_evidence_1mb_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
        file_1mb: bytes,
    ):
        """Measure latency of downloading 1MB evidence.

        Target: p95 < 400ms
        """
        # Setup - upload evidence
        uploaded = await evidence_service.upload_evidence(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
            user_id="benchmark-user",
            file_data=file_1mb,
            original_filename="benchmark_download.bin",
            mime_type="application/octet-stream",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        # Benchmark download
        start = time.perf_counter()
        data, filename, mime = await evidence_service.download_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=benchmark_case.organization_id,
        )
        latency = time.perf_counter() - start

        assert len(data) == len(file_1mb)
        assert latency < 0.400, (
            f"Download 1MB latency {latency*1000:.1f}ms exceeds 400ms target"
        )
        print(f"\n  Download 1MB evidence latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestEvidenceDeletePerformance:
    """Benchmark evidence delete operations."""

    @pytest.mark.asyncio
    async def test_delete_evidence_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
        file_1mb: bytes,
    ):
        """Measure latency of deleting evidence.

        Target: p95 < 200ms
        """
        # Setup - upload evidence
        uploaded = await evidence_service.upload_evidence(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
            user_id="benchmark-user",
            file_data=file_1mb,
            original_filename="benchmark_delete.bin",
            mime_type="application/octet-stream",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        # Benchmark delete
        start = time.perf_counter()
        deleted = await evidence_service.delete_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=benchmark_case.organization_id,
        )
        latency = time.perf_counter() - start

        assert deleted is True
        assert latency < 0.200, (
            f"Delete evidence latency {latency*1000:.1f}ms exceeds 200ms target"
        )
        print(f"\n  Delete evidence latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestEvidenceListPerformance:
    """Benchmark evidence list operations."""

    @pytest.mark.asyncio
    async def test_list_evidence_50_artifacts_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
    ):
        """Measure latency of listing 50 evidence artifacts.

        Target: p95 < 300ms
        """
        # Setup - create 50 evidence artifacts (small data to focus on listing)
        small_data = b"x" * 100
        for i in range(50):
            await evidence_service.upload_evidence(
                case_id=benchmark_case.case_id,
                organization_id=benchmark_case.organization_id,
                user_id="benchmark-user",
                file_data=small_data,
                original_filename=f"file_{i:03d}.txt",
                mime_type="text/plain",
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )

        # Benchmark list
        start = time.perf_counter()
        result = await evidence_service.list_evidence_by_case(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
            limit=50,
        )
        latency = time.perf_counter() - start

        assert len(result) == 50
        assert latency < 0.300, (
            f"List 50 artifacts latency {latency*1000:.1f}ms exceeds 300ms target"
        )
        print(f"\n  List 50 evidence artifacts latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestPrimaryEvidencePerformance:
    """Benchmark primary evidence operations."""

    @pytest.mark.asyncio
    async def test_set_primary_evidence_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
    ):
        """Measure latency of setting primary evidence.

        Target: p95 < 150ms
        """
        # Setup - create 10 evidence artifacts
        small_data = b"x" * 100
        evidence_ids = []
        for i in range(10):
            result = await evidence_service.upload_evidence(
                case_id=benchmark_case.case_id,
                organization_id=benchmark_case.organization_id,
                user_id="benchmark-user",
                file_data=small_data,
                original_filename=f"primary_test_{i}.txt",
                mime_type="text/plain",
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )
            evidence_ids.append(result.evidence_id)

        # Benchmark set primary
        start = time.perf_counter()
        result = await evidence_service.set_primary_evidence(
            evidence_id=evidence_ids[5],
            organization_id=benchmark_case.organization_id,
        )
        latency = time.perf_counter() - start

        assert result.is_primary is True
        assert latency < 0.150, (
            f"Set primary latency {latency*1000:.1f}ms exceeds 150ms target"
        )
        print(f"\n  Set primary evidence latency: {latency*1000:.1f}ms")


@pytest.mark.benchmark
class TestEvidenceStatisticsPerformance:
    """Benchmark evidence statistics operations."""

    @pytest.mark.asyncio
    async def test_get_statistics_100_artifacts_latency(
        self,
        evidence_service: APIEvidenceArtifactService,
        benchmark_case: Case,
    ):
        """Measure latency of getting statistics for 100 artifacts.

        Target: p95 < 400ms
        """
        # Setup - create 100 evidence artifacts
        small_data = b"x" * 1000  # 1KB each
        for i in range(100):
            evidence_type = (
                EvidenceArtifactType.SCREENSHOT
                if i % 3 == 0
                else EvidenceArtifactType.LOG_FILE
                if i % 3 == 1
                else EvidenceArtifactType.CONFIGURATION
            )
            await evidence_service.upload_evidence(
                case_id=benchmark_case.case_id,
                organization_id=benchmark_case.organization_id,
                user_id="benchmark-user",
                file_data=small_data,
                original_filename=f"stats_test_{i:03d}.bin",
                mime_type="application/octet-stream",
                evidence_type=evidence_type,
            )

        # Benchmark get statistics
        start = time.perf_counter()
        stats = await evidence_service.get_evidence_statistics(
            case_id=benchmark_case.case_id,
            organization_id=benchmark_case.organization_id,
        )
        latency = time.perf_counter() - start

        assert stats["total_artifacts"] == 100
        assert latency < 0.400, (
            f"Get statistics latency {latency*1000:.1f}ms exceeds 400ms target"
        )
        print(f"\n  Get statistics (100 artifacts) latency: {latency*1000:.1f}ms")
