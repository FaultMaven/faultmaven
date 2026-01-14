"""Unit tests for CaseRepository standalone evidence methods.

Tests the new standalone evidence persistence methods added to CaseRepository:
- create_standalone_evidence
- get_standalone_evidence
- list_standalone_evidence
- delete_standalone_evidence
- link_standalone_evidence_to_case

Tests InMemoryCaseRepository implementation.

Run with:
    pytest tests/unit/infrastructure/persistence/test_case_repository_standalone_evidence.py -v
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceListFilter,
    StorageBackend,
)
from tests.utils import generate_case_id


@pytest.fixture
def inmemory_repository() -> InMemoryCaseRepository:
    """Create InMemoryCaseRepository instance."""
    return InMemoryCaseRepository()


# ============================================================
# Test Standalone Evidence Creation
# ============================================================


@pytest.mark.asyncio
async def test_create_standalone_evidence_success(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test creating standalone evidence successfully."""
    uploaded_by = str(uuid4())
    evidence = await inmemory_repository.create_standalone_evidence(
        filename="test_image.png",
        content_type="image/png",
        size_bytes=1024000,
        storage_path="/evidence/test_image.png",
        uploaded_by=uploaded_by,
        description="Test screenshot",
        tags=["screenshot", "test"],
    )

    assert evidence is not None
    assert evidence.evidence_id is not None
    assert evidence.original_filename == "test_image.png"
    assert evidence.mime_type == "image/png"
    assert evidence.file_size == 1024000
    assert evidence.user_id == uploaded_by
    assert evidence.description == "Test screenshot"
    assert set(evidence.tags) == {"screenshot", "test"}
    assert evidence.case_id == "standalone"
    assert evidence.organization_id == "default_org"
    assert evidence.evidence_type == EvidenceArtifactType.SCREENSHOT
    assert evidence.storage_backend == StorageBackend.LOCAL_FILESYSTEM
    assert len(evidence.linked_case_ids) == 0


@pytest.mark.asyncio
async def test_create_standalone_evidence_infers_type(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test that evidence type is inferred from content_type."""
    uploaded_by = str(uuid4())
    # Image
    image_evidence = await inmemory_repository.create_standalone_evidence(
        filename="screenshot.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/screenshot.png",
        uploaded_by=uploaded_by,
    )
    assert image_evidence.evidence_type == EvidenceArtifactType.SCREENSHOT

    # Video
    video_evidence = await inmemory_repository.create_standalone_evidence(
        filename="recording.mp4",
        content_type="video/mp4",
        size_bytes=2048,
        storage_path="/evidence/recording.mp4",
        uploaded_by=uploaded_by,
    )
    assert video_evidence.evidence_type == EvidenceArtifactType.VIDEO_RECORDING

    # Text/JSON
    log_evidence = await inmemory_repository.create_standalone_evidence(
        filename="logs.json",
        content_type="application/json",
        size_bytes=4096,
        storage_path="/evidence/logs.json",
        uploaded_by=uploaded_by,
    )
    assert log_evidence.evidence_type == EvidenceArtifactType.LOG_FILE

    # HAR file
    har_evidence = await inmemory_repository.create_standalone_evidence(
        filename="network.har",
        content_type="application/x-har",
        size_bytes=8192,
        storage_path="/evidence/network.har",
        uploaded_by=uploaded_by,
    )
    assert har_evidence.evidence_type == EvidenceArtifactType.HAR_FILE

    # Other
    other_evidence = await inmemory_repository.create_standalone_evidence(
        filename="unknown.bin",
        content_type="application/octet-stream",
        size_bytes=512,
        storage_path="/evidence/unknown.bin",
        uploaded_by=uploaded_by,
    )
    assert other_evidence.evidence_type == EvidenceArtifactType.OTHER


@pytest.mark.asyncio
async def test_create_standalone_evidence_without_tags(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test creating standalone evidence without tags."""
    uploaded_by = str(uuid4())
    evidence = await inmemory_repository.create_standalone_evidence(
        filename="test.txt",
        content_type="text/plain",
        size_bytes=512,
        storage_path="/evidence/test.txt",
        uploaded_by=uploaded_by,
    )

    assert evidence.tags == []


# ============================================================
# Test Get Standalone Evidence
# ============================================================


@pytest.mark.asyncio
async def test_get_standalone_evidence_success(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test getting standalone evidence by ID."""
    uploaded_by = str(uuid4())
    created = await inmemory_repository.create_standalone_evidence(
        filename="test.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test.png",
        uploaded_by=uploaded_by,
    )

    retrieved = await inmemory_repository.get_standalone_evidence(created.evidence_id)

    assert retrieved is not None
    assert retrieved.evidence_id == created.evidence_id
    assert retrieved.original_filename == "test.png"


@pytest.mark.asyncio
async def test_get_standalone_evidence_not_found(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test getting non-existent standalone evidence."""
    result = await inmemory_repository.get_standalone_evidence("nonexistent-id")
    assert result is None


# ============================================================
# Test List Standalone Evidence
# ============================================================


@pytest.mark.asyncio
async def test_list_standalone_evidence_all(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test listing all standalone evidence."""
    uploaded_by = str(uuid4())
    # Create multiple evidence items
    evidence1 = await inmemory_repository.create_standalone_evidence(
        filename="test1.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test1.png",
        uploaded_by=uploaded_by,
        tags=["tag1"],
    )
    evidence2 = await inmemory_repository.create_standalone_evidence(
        filename="test2.jpg",
        content_type="image/jpeg",
        size_bytes=2048,
        storage_path="/evidence/test2.jpg",
        uploaded_by=uploaded_by,
        tags=["tag2"],
    )

    filters = EvidenceListFilter()
    results, total = await inmemory_repository.list_standalone_evidence(filters)

    assert total >= 2
    evidence_ids = {e.evidence_id for e in results}
    assert evidence1.evidence_id in evidence_ids
    assert evidence2.evidence_id in evidence_ids


@pytest.mark.asyncio
async def test_list_standalone_evidence_filter_by_tags(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test filtering standalone evidence by tags."""
    uploaded_by = str(uuid4())
    evidence1 = await inmemory_repository.create_standalone_evidence(
        filename="test1.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test1.png",
        uploaded_by=uploaded_by,
        tags=["screenshot", "bug"],
    )
    evidence2 = await inmemory_repository.create_standalone_evidence(
        filename="test2.png",
        content_type="image/png",
        size_bytes=2048,
        storage_path="/evidence/test2.png",
        uploaded_by=uploaded_by,
        tags=["screenshot", "test"],
    )
    evidence3 = await inmemory_repository.create_standalone_evidence(
        filename="test3.txt",
        content_type="text/plain",
        size_bytes=512,
        storage_path="/evidence/test3.txt",
        uploaded_by=uploaded_by,
        tags=["log", "error"],
    )

    filters = EvidenceListFilter(tags=["screenshot"])
    results, total = await inmemory_repository.list_standalone_evidence(filters)

    evidence_ids = {e.evidence_id for e in results}
    assert evidence1.evidence_id in evidence_ids
    assert evidence2.evidence_id in evidence_ids
    assert evidence3.evidence_id not in evidence_ids


@pytest.mark.asyncio
async def test_list_standalone_evidence_filter_by_case_id(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test filtering standalone evidence by linked case_id.

    NOTE: There's a known bug in the implementation where filters.case_id (UUID)
    is compared directly with linked_case_ids (strings), which will fail.
    This test documents the issue and will need to be fixed when the bug is resolved.
    """
    uploaded_by = str(uuid4())
    case_id1 = str(uuid4())  # Use UUID string format
    case_id2 = str(uuid4())

    evidence1 = await inmemory_repository.create_standalone_evidence(
        filename="test1.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test1.png",
        uploaded_by=uploaded_by,
    )
    # Link evidence1 to case_id1
    await inmemory_repository.link_standalone_evidence_to_case(
        evidence1.evidence_id, case_id1
    )

    evidence2 = await inmemory_repository.create_standalone_evidence(
        filename="test2.png",
        content_type="image/png",
        size_bytes=2048,
        storage_path="/evidence/test2.png",
        uploaded_by=uploaded_by,
    )
    # Link evidence2 to case_id2
    await inmemory_repository.link_standalone_evidence_to_case(
        evidence2.evidence_id, case_id2
    )

    # Test filtering by case_id (EvidenceListFilter expects str, not UUID)
    filters = EvidenceListFilter(case_id=case_id1)
    results, total = await inmemory_repository.list_standalone_evidence(filters)

    # Filter should work correctly (implementation converts UUID to string)
    evidence_ids = {e.evidence_id for e in results}
    assert evidence1.evidence_id in evidence_ids
    assert evidence2.evidence_id not in evidence_ids


@pytest.mark.asyncio
async def test_list_standalone_evidence_filter_by_uploaded_by(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test filtering standalone evidence by uploaded_by."""
    user1 = str(uuid4())
    user2 = str(uuid4())

    evidence1 = await inmemory_repository.create_standalone_evidence(
        filename="test1.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test1.png",
        uploaded_by=user1,
    )
    evidence2 = await inmemory_repository.create_standalone_evidence(
        filename="test2.png",
        content_type="image/png",
        size_bytes=2048,
        storage_path="/evidence/test2.png",
        uploaded_by=user2,
    )

    filters = EvidenceListFilter(uploaded_by=UUID(user1))
    results, total = await inmemory_repository.list_standalone_evidence(filters)

    # Implementation uses str(filters.uploaded_by), so this should work
    evidence_ids = {e.evidence_id for e in results}
    assert evidence1.evidence_id in evidence_ids
    assert evidence2.evidence_id not in evidence_ids


@pytest.mark.asyncio
async def test_list_standalone_evidence_filter_by_filename(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test filtering standalone evidence by filename contains."""
    uploaded_by = str(uuid4())
    evidence1 = await inmemory_repository.create_standalone_evidence(
        filename="screenshot_bug.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/screenshot_bug.png",
        uploaded_by=uploaded_by,
    )
    evidence2 = await inmemory_repository.create_standalone_evidence(
        filename="error_log.txt",
        content_type="text/plain",
        size_bytes=512,
        storage_path="/evidence/error_log.txt",
        uploaded_by=uploaded_by,
    )

    filters = EvidenceListFilter(filename_contains="bug")
    results, total = await inmemory_repository.list_standalone_evidence(filters)

    evidence_ids = {e.evidence_id for e in results}
    assert evidence1.evidence_id in evidence_ids
    assert evidence2.evidence_id not in evidence_ids


@pytest.mark.asyncio
async def test_list_standalone_evidence_pagination(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test pagination for listing standalone evidence."""
    uploaded_by = str(uuid4())
    # Create 5 evidence items
    for i in range(5):
        await inmemory_repository.create_standalone_evidence(
            filename=f"test{i}.png",
            content_type="image/png",
            size_bytes=1024,
            storage_path=f"/evidence/test{i}.png",
            uploaded_by=uploaded_by,
        )

    filters = EvidenceListFilter(limit=2, offset=0)
    page1, total = await inmemory_repository.list_standalone_evidence(filters)
    assert len(page1) == 2
    assert total >= 5

    filters = EvidenceListFilter(limit=2, offset=2)
    page2, total2 = await inmemory_repository.list_standalone_evidence(filters)
    assert len(page2) == 2
    assert total2 == total  # Total should be the same


# ============================================================
# Test Delete Standalone Evidence
# ============================================================


@pytest.mark.asyncio
async def test_delete_standalone_evidence_success(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test deleting standalone evidence."""
    uploaded_by = str(uuid4())
    evidence = await inmemory_repository.create_standalone_evidence(
        filename="test.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test.png",
        uploaded_by=uploaded_by,
    )

    deleted = await inmemory_repository.delete_standalone_evidence(evidence.evidence_id)
    assert deleted is True

    retrieved = await inmemory_repository.get_standalone_evidence(evidence.evidence_id)
    assert retrieved is None


@pytest.mark.asyncio
async def test_delete_standalone_evidence_not_found(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test deleting non-existent standalone evidence."""
    deleted = await inmemory_repository.delete_standalone_evidence("nonexistent-id")
    assert deleted is False


# ============================================================
# Test Link Standalone Evidence to Case
# ============================================================


@pytest.mark.asyncio
async def test_link_standalone_evidence_to_case_success(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test linking standalone evidence to a case."""
    uploaded_by = str(uuid4())
    case_id = generate_case_id()
    evidence = await inmemory_repository.create_standalone_evidence(
        filename="test.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test.png",
        uploaded_by=uploaded_by,
    )

    assert evidence.case_id == "standalone"
    assert len(evidence.linked_case_ids) == 0

    linked = await inmemory_repository.link_standalone_evidence_to_case(
        evidence.evidence_id, case_id
    )

    assert linked is not None
    assert linked.case_id == case_id  # Primary case is first linked case
    assert case_id in linked.linked_case_ids
    assert len(linked.linked_case_ids) == 1

    # Verify stored evidence is updated
    retrieved = await inmemory_repository.get_standalone_evidence(evidence.evidence_id)
    assert retrieved is not None
    assert case_id in retrieved.linked_case_ids


@pytest.mark.asyncio
async def test_link_standalone_evidence_to_multiple_cases(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test linking standalone evidence to multiple cases."""
    uploaded_by = str(uuid4())
    case_id1 = generate_case_id()
    case_id2 = generate_case_id()

    evidence = await inmemory_repository.create_standalone_evidence(
        filename="test.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test.png",
        uploaded_by=uploaded_by,
    )

    # Link to first case
    linked1 = await inmemory_repository.link_standalone_evidence_to_case(
        evidence.evidence_id, case_id1
    )
    assert case_id1 in linked1.linked_case_ids
    assert linked1.case_id == case_id1  # Primary case

    # Link to second case
    linked2 = await inmemory_repository.link_standalone_evidence_to_case(
        evidence.evidence_id, case_id2
    )
    assert case_id1 in linked2.linked_case_ids
    assert case_id2 in linked2.linked_case_ids
    assert linked2.case_id == case_id1  # Primary case remains first


@pytest.mark.asyncio
async def test_link_standalone_evidence_to_case_duplicate(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test linking standalone evidence to the same case twice (should be idempotent)."""
    uploaded_by = str(uuid4())
    case_id = generate_case_id()
    evidence = await inmemory_repository.create_standalone_evidence(
        filename="test.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test.png",
        uploaded_by=uploaded_by,
    )

    # Link first time
    linked1 = await inmemory_repository.link_standalone_evidence_to_case(
        evidence.evidence_id, case_id
    )
    assert len(linked1.linked_case_ids) == 1

    # Link second time (should not duplicate)
    linked2 = await inmemory_repository.link_standalone_evidence_to_case(
        evidence.evidence_id, case_id
    )
    assert len(linked2.linked_case_ids) == 1  # Should still be 1, not 2


@pytest.mark.asyncio
async def test_link_standalone_evidence_to_case_not_found(
    inmemory_repository: InMemoryCaseRepository,
):
    """Test linking non-existent standalone evidence to a case."""
    case_id = generate_case_id()
    result = await inmemory_repository.link_standalone_evidence_to_case(
        "nonexistent-id", case_id
    )
    assert result is None
