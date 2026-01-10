"""Integration tests for Evidence Artifact Repository.

Tests the full case-evidence relationship with real database operations.

Run with:
    pytest tests/integration/test_evidence_artifact_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - PostgreSQL for production testing (optional, requires DATABASE_URL)
"""

import os
import pytest
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.database import reset_engine
from faultmaven.infrastructure.persistence.database_case_repository import (
    DatabaseCaseRepository,
)
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    DatabaseEvidenceArtifactRepository,
    InMemoryEvidenceArtifactRepository,
)
from faultmaven.infrastructure.persistence.repository_factory import (
    get_evidence_artifact_repository_async,
    reset_inmemory_evidence_artifact_repository,
    STORAGE_TYPE_INMEMORY,
    STORAGE_TYPE_DATABASE,
)
from faultmaven.modules.case.domain.models import Case, CaseStatus, InvestigationStrategy, ConsultingData
from faultmaven.modules.evidence.domain.models import (
from tests.utils import generate_case_id, generate_evidence_id
    EvidenceArtifact,
    EvidenceArtifactType,
    StorageBackend,
)
from tests.utils import generate_case_id, generate_evidence_id


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture(scope="function")
async def test_engine():
    """Create test engine with in-memory SQLite with foreign key constraints enabled."""
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    reset_engine()

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    # Enable foreign key constraints for SQLite
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()
    reset_engine()


@pytest.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test session."""
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture(scope="function")
async def case_repository(test_session) -> DatabaseCaseRepository:
    """Create DatabaseCaseRepository with test session."""
    return DatabaseCaseRepository(test_session)


@pytest.fixture(scope="function")
async def evidence_repository(test_session) -> DatabaseEvidenceArtifactRepository:
    """Create DatabaseEvidenceArtifactRepository with test session."""
    return DatabaseEvidenceArtifactRepository(test_session)


@pytest.fixture(scope="function")
def inmemory_repository() -> InMemoryEvidenceArtifactRepository:
    """Create fresh InMemoryEvidenceArtifactRepository."""
    reset_inmemory_evidence_artifact_repository()
    return InMemoryEvidenceArtifactRepository()


@pytest.fixture
async def sample_case(case_repository: DatabaseCaseRepository) -> Case:
    """Create a sample case for testing evidence."""
    case = Case(
        case_id=generate_case_id(),
        user_id="integration-test-user",
        organization_id="integration-test-org",
        title="Evidence Integration Test Case",
        description="Testing evidence management",
        status=CaseStatus.INVESTIGATING,
        consulting=ConsultingData(
            proposed_problem_statement="Test problem statement",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    return await case_repository.save(case)


def create_sample_evidence(case_id: str) -> EvidenceArtifact:
    """Create a sample evidence artifact for testing."""
    return EvidenceArtifact(
        evidence_id=generate_evidence_id(),
        case_id=case_id,
        user_id="integration-test-user",
        organization_id="integration-test-org",
        original_filename="screenshot.png",
        stored_filename=f"{generate_evidence_id()}.png",
        file_path="/evidence/2025/12/screenshot.png",
        evidence_type=EvidenceArtifactType.SCREENSHOT,
        mime_type="image/png",
        file_size=1024000,
        storage_backend=StorageBackend.LOCAL_FILESYSTEM,
    )


# ============================================================
# Full Evidence Lifecycle Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_evidence_lifecycle(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test create -> update -> retrieve -> delete flow."""
    # Step 1: Create evidence
    evidence = create_sample_evidence(sample_case.case_id)
    created = await evidence_repository.create_evidence(evidence)

    assert created.evidence_id == evidence.evidence_id
    assert created.case_id == sample_case.case_id

    # Step 2: Retrieve evidence
    retrieved = await evidence_repository.get_evidence(evidence.evidence_id)
    assert retrieved is not None
    assert retrieved.original_filename == "screenshot.png"

    # Step 3: Update evidence
    evidence.description = "Updated screenshot description"
    evidence.is_primary = True
    updated = await evidence_repository.update_evidence(evidence)
    assert updated.description == "Updated screenshot description"
    assert updated.is_primary is True

    # Verify update persisted
    retrieved_updated = await evidence_repository.get_evidence(evidence.evidence_id)
    assert retrieved_updated.description == "Updated screenshot description"
    assert retrieved_updated.is_primary is True

    # Step 4: Delete evidence
    deleted = await evidence_repository.delete_evidence(evidence.evidence_id)
    assert deleted is True

    # Verify deletion
    final = await evidence_repository.get_evidence(evidence.evidence_id)
    assert final is None


# ============================================================
# Case-Evidence Relationship Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evidence_preserves_case_foreign_key(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test that evidence maintains foreign key to case."""
    evidence = create_sample_evidence(sample_case.case_id)
    await evidence_repository.create_evidence(evidence)

    retrieved = await evidence_repository.get_evidence(evidence.evidence_id)
    assert retrieved.case_id == sample_case.case_id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evidence_invalid_case_id_fails(
    evidence_repository: DatabaseEvidenceArtifactRepository,
):
    """Test that creating evidence with invalid case_id fails."""
    evidence = create_sample_evidence("case_nonexistent")

    with pytest.raises(ValueError, match="not found"):
        await evidence_repository.create_evidence(evidence)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_evidence_per_case(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test that a case can have multiple evidence items."""
    # Create 5 evidence items
    for i in range(5):
        evidence = create_sample_evidence(sample_case.case_id)
        evidence.original_filename = f"evidence_{i}.png"
        await evidence_repository.create_evidence(evidence)

    # Verify all created
    result, total = await evidence_repository.list_evidence_by_case(
        sample_case.case_id
    )
    assert len(result) == 5
    assert total == 5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evidence_cascade_delete_on_case_delete(
    case_repository: DatabaseCaseRepository,
    evidence_repository: DatabaseEvidenceArtifactRepository,
):
    """Test that evidence is deleted when case is deleted (CASCADE)."""
    # Create case
    case = Case(
        case_id=generate_case_id(),
        user_id="cascade-test-user",
        organization_id="cascade-test-org",
        title="Cascade Delete Test",
    )
    await case_repository.save(case)

    # Create evidence
    evidence = create_sample_evidence(case.case_id)
    await evidence_repository.create_evidence(evidence)

    # Verify evidence exists
    retrieved = await evidence_repository.get_evidence(evidence.evidence_id)
    assert retrieved is not None

    # Delete case
    await case_repository.delete(case.case_id)

    # Verify evidence is deleted (CASCADE)
    retrieved_after = await evidence_repository.get_evidence(evidence.evidence_id)
    assert retrieved_after is None


# ============================================================
# Evidence Query Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_evidence_with_type_filter(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test filtering evidence by type."""
    # Create different types of evidence
    types = [
        EvidenceArtifactType.SCREENSHOT,
        EvidenceArtifactType.SCREENSHOT,
        EvidenceArtifactType.LOG_FILE,
        EvidenceArtifactType.NETWORK_TRACE,
    ]

    for i, etype in enumerate(types):
        evidence = EvidenceArtifact(
            evidence_id=generate_evidence_id(),
            case_id=sample_case.case_id,
            user_id="integration-test-user",
            organization_id="integration-test-org",
            original_filename=f"file_{i}.txt",
            stored_filename=f"{generate_evidence_id()}.txt",
            file_path=f"/evidence/file_{i}.txt",
            evidence_type=etype,
            mime_type="text/plain",
            file_size=1000,
        )
        await evidence_repository.create_evidence(evidence)

    # Filter by screenshot
    screenshots, total = await evidence_repository.list_evidence_by_case(
        sample_case.case_id,
        evidence_type=EvidenceArtifactType.SCREENSHOT,
    )
    assert len(screenshots) == 2
    assert total == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_evidence_pagination(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test evidence list pagination."""
    # Create 20 evidence items
    for i in range(20):
        evidence = create_sample_evidence(sample_case.case_id)
        await evidence_repository.create_evidence(evidence)

    # Get first page
    page1, total = await evidence_repository.list_evidence_by_case(
        sample_case.case_id,
        limit=5,
        offset=0,
    )
    assert len(page1) == 5
    assert total == 20

    # Get second page
    page2, total = await evidence_repository.list_evidence_by_case(
        sample_case.case_id,
        limit=5,
        offset=5,
    )
    assert len(page2) == 5
    assert total == 20

    # Verify no overlap
    page1_ids = {e.evidence_id for e in page1}
    page2_ids = {e.evidence_id for e in page2}
    assert page1_ids.isdisjoint(page2_ids)


# ============================================================
# Primary Evidence Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_primary_evidence_workflow(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test setting and getting primary evidence."""
    # Create two evidence items
    evidence1 = create_sample_evidence(sample_case.case_id)
    evidence2 = create_sample_evidence(sample_case.case_id)
    await evidence_repository.create_evidence(evidence1)
    await evidence_repository.create_evidence(evidence2)

    # Initially no primary
    primary = await evidence_repository.get_primary_evidence(sample_case.case_id)
    assert primary is None

    # Set evidence1 as primary
    result = await evidence_repository.set_primary_evidence(
        sample_case.case_id,
        evidence1.evidence_id,
    )
    assert result is True

    # Verify evidence1 is primary
    primary = await evidence_repository.get_primary_evidence(sample_case.case_id)
    assert primary.evidence_id == evidence1.evidence_id

    # Set evidence2 as primary (should unset evidence1)
    result = await evidence_repository.set_primary_evidence(
        sample_case.case_id,
        evidence2.evidence_id,
    )
    assert result is True

    # Verify evidence2 is now primary
    primary = await evidence_repository.get_primary_evidence(sample_case.case_id)
    assert primary.evidence_id == evidence2.evidence_id

    # Verify evidence1 is no longer primary
    retrieved1 = await evidence_repository.get_evidence(evidence1.evidence_id)
    assert retrieved1.is_primary is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_primary_evidence_wrong_case_fails(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    case_repository: DatabaseCaseRepository,
):
    """Test that setting evidence as primary for wrong case fails."""
    # Create two cases
    case1 = Case(
        case_id=generate_case_id(),
        user_id="test-user",
        organization_id="test-org",
        title="Case 1",
    )
    case2 = Case(
        case_id=generate_case_id(),
        user_id="test-user",
        organization_id="test-org",
        title="Case 2",
    )
    await case_repository.save(case1)
    await case_repository.save(case2)

    # Create evidence for case1
    evidence = create_sample_evidence(case1.case_id)
    await evidence_repository.create_evidence(evidence)

    # Try to set as primary for case2
    result = await evidence_repository.set_primary_evidence(
        case2.case_id,
        evidence.evidence_id,
    )
    assert result is False


# ============================================================
# Data Persistence Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evidence_with_large_metadata(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test evidence with large metadata JSON."""
    large_metadata = {
        "source": "kubernetes",
        "pod": "api-server-deployment-abc123",
        "container": "api-server",
        "namespace": "production",
        "labels": {f"label_{i}": f"value_{i}" for i in range(50)},
        "annotations": {f"annotation_{i}": f"value_{i}" for i in range(50)},
        "environment": {
            "DATABASE_URL": "postgres://...",
            "REDIS_URL": "redis://...",
        },
    }

    evidence = create_sample_evidence(sample_case.case_id)
    evidence.metadata = large_metadata
    await evidence_repository.create_evidence(evidence)

    # Retrieve and verify
    retrieved = await evidence_repository.get_evidence(evidence.evidence_id)
    assert retrieved.metadata == large_metadata
    assert len(retrieved.metadata["labels"]) == 50


@pytest.mark.asyncio
@pytest.mark.integration
async def test_evidence_with_unicode_filenames(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test evidence with unicode characters in filenames."""
    unicode_filenames = [
        "스크린샷_2025.png",  # Korean
        "截图_2025.png",  # Chinese
        "スクリーンショット_2025.png",  # Japanese
        "Снимок_экрана_2025.png",  # Russian
        "écran_capture_2025.png",  # French accents
    ]

    for filename in unicode_filenames:
        evidence = EvidenceArtifact(
            evidence_id=generate_evidence_id(),
            case_id=sample_case.case_id,
            user_id="test-user",
            organization_id="test-org",
            original_filename=filename,
            stored_filename=f"{generate_evidence_id()}.png",
            file_path=f"/evidence/{filename}",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=1000,
        )
        await evidence_repository.create_evidence(evidence)

    # Verify all stored correctly
    result, total = await evidence_repository.list_evidence_by_case(sample_case.case_id)
    assert total == len(unicode_filenames)

    stored_filenames = {e.original_filename for e in result}
    assert stored_filenames == set(unicode_filenames)


# ============================================================
# Repository Factory Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_inmemory():
    """Test repository factory returns InMemoryEvidenceArtifactRepository."""
    os.environ["EVIDENCE_ARTIFACT_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY
    reset_inmemory_evidence_artifact_repository()

    async with get_evidence_artifact_repository_async() as repo:
        assert isinstance(repo, InMemoryEvidenceArtifactRepository)

    # Cleanup
    reset_inmemory_evidence_artifact_repository()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_repository_factory_invalid_type():
    """Test repository factory with invalid storage type."""
    os.environ["EVIDENCE_ARTIFACT_STORAGE_TYPE"] = "invalid_type"

    with pytest.raises(ValueError, match="Unknown storage type"):
        async with get_evidence_artifact_repository_async() as repo:
            pass

    # Reset
    os.environ["EVIDENCE_ARTIFACT_STORAGE_TYPE"] = STORAGE_TYPE_INMEMORY


# ============================================================
# Error Handling Tests
# ============================================================


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_nonexistent_evidence(
    evidence_repository: DatabaseEvidenceArtifactRepository,
):
    """Test getting evidence that doesn't exist."""
    result = await evidence_repository.get_evidence("ev_doesnotexist")
    assert result is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_nonexistent_evidence(
    evidence_repository: DatabaseEvidenceArtifactRepository,
):
    """Test deleting evidence that doesn't exist."""
    result = await evidence_repository.delete_evidence("ev_doesnotexist")
    assert result is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_nonexistent_evidence(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test updating evidence that doesn't exist."""
    evidence = create_sample_evidence(sample_case.case_id)

    with pytest.raises(ValueError, match="not found"):
        await evidence_repository.update_evidence(evidence)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_duplicate_evidence_id_fails(
    evidence_repository: DatabaseEvidenceArtifactRepository,
    sample_case: Case,
):
    """Test creating evidence with duplicate ID fails."""
    evidence = create_sample_evidence(sample_case.case_id)
    await evidence_repository.create_evidence(evidence)

    duplicate = create_sample_evidence(sample_case.case_id)
    duplicate.evidence_id = evidence.evidence_id

    with pytest.raises(ValueError, match="already exists"):
        await evidence_repository.create_evidence(duplicate)
