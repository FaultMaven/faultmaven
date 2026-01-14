"""Unit tests for Evidence Artifact Repository.

Tests the InMemoryEvidenceArtifactRepository implementation.
"""

import pytest
from datetime import datetime, timezone

from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    StorageBackend,
)
from faultmaven.infrastructure.persistence.evidence_artifact_repository import (
    InMemoryEvidenceArtifactRepository,
)
from tests.utils import generate_case_id, generate_evidence_id


def create_sample_evidence(
    case_id: str = None,
    evidence_type: EvidenceArtifactType = EvidenceArtifactType.SCREENSHOT,
    is_primary: bool = False,
) -> EvidenceArtifact:
    """Create a sample evidence artifact for testing."""
    return EvidenceArtifact(
        evidence_id=generate_evidence_id(),
        case_id=case_id or generate_case_id(),
        user_id="test-user-001",
        organization_id="test-org-001",
        original_filename="test_file.png",
        stored_filename=f"{generate_evidence_id()}.png",
        file_path="/evidence/2025/12/test_file.png",
        evidence_type=evidence_type,
        mime_type="image/png",
        file_size=1024000,
        storage_backend=StorageBackend.LOCAL_FILESYSTEM,
        is_primary=is_primary,
    )


@pytest.fixture
def repository():
    """Create a fresh in-memory repository for each test."""
    return InMemoryEvidenceArtifactRepository()


class TestEvidenceCreation:
    """Tests for evidence artifact creation."""

    @pytest.mark.asyncio
    async def test_create_evidence_success(self, repository):
        """Test creating an evidence artifact successfully."""
        evidence = create_sample_evidence()

        result = await repository.create_evidence(evidence)

        assert result is not None
        assert result.evidence_id == evidence.evidence_id
        assert result.case_id == evidence.case_id
        assert result.original_filename == evidence.original_filename

    @pytest.mark.asyncio
    async def test_create_evidence_duplicate_id_fails(self, repository):
        """Test that creating evidence with duplicate ID fails."""
        evidence = create_sample_evidence()
        await repository.create_evidence(evidence)

        # Try to create with same ID
        duplicate = create_sample_evidence()
        duplicate.evidence_id = evidence.evidence_id

        with pytest.raises(ValueError, match="already exists"):
            await repository.create_evidence(duplicate)

    @pytest.mark.asyncio
    async def test_create_evidence_preserves_all_fields(self, repository):
        """Test that all fields are preserved when creating evidence."""
        evidence = EvidenceArtifact(
            evidence_id="ev_fullfields123",
            case_id="case_123",
            user_id="user_001",
            organization_id="org_001",
            original_filename="log.txt",
            stored_filename="ev_fullfields123.txt",
            file_path="/evidence/log.txt",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            mime_type="text/plain",
            file_size=2048,
            storage_backend=StorageBackend.S3,
            metadata={"source": "kubernetes"},
            description="Kubernetes pod logs",
            is_primary=True,
        )

        result = await repository.create_evidence(evidence)

        assert result.storage_backend == StorageBackend.S3
        assert result.metadata == {"source": "kubernetes"}
        assert result.description == "Kubernetes pod logs"
        assert result.is_primary is True


class TestEvidenceRetrieval:
    """Tests for evidence artifact retrieval."""

    @pytest.mark.asyncio
    async def test_get_evidence_found(self, repository):
        """Test retrieving an existing evidence artifact."""
        evidence = create_sample_evidence()
        await repository.create_evidence(evidence)

        result = await repository.get_evidence(evidence.evidence_id)

        assert result is not None
        assert result.evidence_id == evidence.evidence_id

    @pytest.mark.asyncio
    async def test_get_evidence_not_found(self, repository):
        """Test retrieving a non-existent evidence artifact."""
        result = await repository.get_evidence("ev_nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_evidence_returns_copy(self, repository):
        """Test that get_evidence returns a copy, not the original."""
        evidence = create_sample_evidence()
        await repository.create_evidence(evidence)

        result1 = await repository.get_evidence(evidence.evidence_id)
        result1.description = "Modified"

        result2 = await repository.get_evidence(evidence.evidence_id)

        assert result2.description is None  # Original unchanged


class TestEvidenceUpdate:
    """Tests for evidence artifact updates."""

    @pytest.mark.asyncio
    async def test_update_evidence_success(self, repository):
        """Test updating an evidence artifact successfully."""
        evidence = create_sample_evidence()
        await repository.create_evidence(evidence)

        evidence.description = "Updated description"
        evidence.is_primary = True

        result = await repository.update_evidence(evidence)

        assert result.description == "Updated description"
        assert result.is_primary is True

    @pytest.mark.asyncio
    async def test_update_evidence_not_found_fails(self, repository):
        """Test that updating non-existent evidence fails."""
        evidence = create_sample_evidence()

        with pytest.raises(ValueError, match="not found"):
            await repository.update_evidence(evidence)

    @pytest.mark.asyncio
    async def test_update_evidence_updates_timestamp(self, repository):
        """Test that update modifies updated_at timestamp."""
        evidence = create_sample_evidence()
        await repository.create_evidence(evidence)

        original_updated = evidence.updated_at

        import time

        time.sleep(0.001)

        evidence.description = "Changed"
        result = await repository.update_evidence(evidence)

        assert result.updated_at > original_updated


class TestEvidenceDeletion:
    """Tests for evidence artifact deletion."""

    @pytest.mark.asyncio
    async def test_delete_evidence_success(self, repository):
        """Test deleting an evidence artifact successfully."""
        evidence = create_sample_evidence()
        await repository.create_evidence(evidence)

        result = await repository.delete_evidence(evidence.evidence_id)

        assert result is True

        # Verify deletion
        retrieved = await repository.get_evidence(evidence.evidence_id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_evidence_not_found(self, repository):
        """Test deleting a non-existent evidence artifact."""
        result = await repository.delete_evidence("ev_nonexistent")

        assert result is False


class TestEvidenceListByCase:
    """Tests for listing evidence by case."""

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_empty(self, repository):
        """Test listing evidence for a case with no evidence."""
        result, total = await repository.list_evidence_by_case("case_empty")

        assert result == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_multiple(self, repository):
        """Test listing multiple evidence for a case."""
        case_id = generate_case_id()

        # Create 5 evidence items
        for i in range(5):
            evidence = create_sample_evidence(case_id=case_id)
            await repository.create_evidence(evidence)

        result, total = await repository.list_evidence_by_case(case_id)

        assert len(result) == 5
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_with_type_filter(self, repository):
        """Test listing evidence filtered by type."""
        case_id = generate_case_id()

        # Create 3 screenshots
        for i in range(3):
            evidence = create_sample_evidence(
                case_id=case_id,
                evidence_type=EvidenceArtifactType.SCREENSHOT,
            )
            await repository.create_evidence(evidence)

        # Create 2 log files
        for i in range(2):
            evidence = create_sample_evidence(
                case_id=case_id,
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )
            await repository.create_evidence(evidence)

        # Filter by screenshot
        screenshots, total = await repository.list_evidence_by_case(
            case_id,
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )

        assert len(screenshots) == 3
        assert total == 3
        assert all(
            e.evidence_type == EvidenceArtifactType.SCREENSHOT for e in screenshots
        )

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_pagination(self, repository):
        """Test pagination of evidence listing."""
        case_id = generate_case_id()

        # Create 10 evidence items
        for i in range(10):
            evidence = create_sample_evidence(case_id=case_id)
            await repository.create_evidence(evidence)

        # Get first page (5 items)
        page1, total = await repository.list_evidence_by_case(
            case_id,
            limit=5,
            offset=0,
        )

        assert len(page1) == 5
        assert total == 10

        # Get second page (5 items)
        page2, total = await repository.list_evidence_by_case(
            case_id,
            limit=5,
            offset=5,
        )

        assert len(page2) == 5
        assert total == 10

        # Ensure no overlap
        page1_ids = {e.evidence_id for e in page1}
        page2_ids = {e.evidence_id for e in page2}
        assert page1_ids.isdisjoint(page2_ids)

    @pytest.mark.asyncio
    async def test_list_evidence_ordered_by_created_at(self, repository):
        """Test that evidence is ordered by created_at descending."""
        case_id = generate_case_id()

        # Create evidence with different timestamps
        import time

        evidence_ids = []
        for i in range(3):
            evidence = create_sample_evidence(case_id=case_id)
            await repository.create_evidence(evidence)
            evidence_ids.append(evidence.evidence_id)
            time.sleep(0.001)

        result, _ = await repository.list_evidence_by_case(case_id)

        # Most recent should be first
        assert result[0].evidence_id == evidence_ids[-1]
        assert result[-1].evidence_id == evidence_ids[0]

    @pytest.mark.asyncio
    async def test_list_evidence_excludes_other_cases(self, repository):
        """Test that listing only returns evidence for specified case."""
        case_id_1 = generate_case_id()
        case_id_2 = generate_case_id()

        # Create evidence for case 1
        for i in range(3):
            evidence = create_sample_evidence(case_id=case_id_1)
            await repository.create_evidence(evidence)

        # Create evidence for case 2
        for i in range(2):
            evidence = create_sample_evidence(case_id=case_id_2)
            await repository.create_evidence(evidence)

        result1, total1 = await repository.list_evidence_by_case(case_id_1)
        result2, total2 = await repository.list_evidence_by_case(case_id_2)

        assert len(result1) == 3
        assert total1 == 3
        assert len(result2) == 2
        assert total2 == 2


class TestPrimaryEvidence:
    """Tests for primary evidence management."""

    @pytest.mark.asyncio
    async def test_get_primary_evidence_none_set(self, repository):
        """Test getting primary evidence when none is set."""
        case_id = generate_case_id()

        # Create non-primary evidence
        evidence = create_sample_evidence(case_id=case_id, is_primary=False)
        await repository.create_evidence(evidence)

        result = await repository.get_primary_evidence(case_id)

        assert result is None

    @pytest.mark.asyncio
    async def test_set_primary_evidence_success(self, repository):
        """Test setting primary evidence successfully."""
        case_id = generate_case_id()

        evidence = create_sample_evidence(case_id=case_id)
        await repository.create_evidence(evidence)

        result = await repository.set_primary_evidence(case_id, evidence.evidence_id)

        assert result is True

        # Verify it's primary
        primary = await repository.get_primary_evidence(case_id)
        assert primary is not None
        assert primary.evidence_id == evidence.evidence_id
        assert primary.is_primary is True

    @pytest.mark.asyncio
    async def test_set_primary_evidence_unsets_previous(self, repository):
        """Test that setting new primary unsets the previous one."""
        case_id = generate_case_id()

        # Create first evidence and set as primary
        evidence1 = create_sample_evidence(case_id=case_id)
        await repository.create_evidence(evidence1)
        await repository.set_primary_evidence(case_id, evidence1.evidence_id)

        # Create second evidence and set as primary
        evidence2 = create_sample_evidence(case_id=case_id)
        await repository.create_evidence(evidence2)
        await repository.set_primary_evidence(case_id, evidence2.evidence_id)

        # Verify only evidence2 is primary
        primary = await repository.get_primary_evidence(case_id)
        assert primary.evidence_id == evidence2.evidence_id

        # Verify evidence1 is no longer primary
        retrieved1 = await repository.get_evidence(evidence1.evidence_id)
        assert retrieved1.is_primary is False

    @pytest.mark.asyncio
    async def test_set_primary_evidence_invalid_evidence_fails(self, repository):
        """Test that setting non-existent evidence as primary fails."""
        case_id = generate_case_id()

        result = await repository.set_primary_evidence(case_id, "ev_nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_set_primary_evidence_wrong_case_fails(self, repository):
        """Test that setting evidence from different case as primary fails."""
        case_id_1 = generate_case_id()
        case_id_2 = generate_case_id()

        # Create evidence for case 1
        evidence = create_sample_evidence(case_id=case_id_1)
        await repository.create_evidence(evidence)

        # Try to set as primary for case 2
        result = await repository.set_primary_evidence(case_id_2, evidence.evidence_id)

        assert result is False


class TestEvidenceCount:
    """Tests for counting evidence by case."""

    @pytest.mark.asyncio
    async def test_count_by_case_empty(self, repository):
        """Test counting evidence for case with no evidence."""
        count = await repository.count_by_case("case_empty")

        assert count == 0

    @pytest.mark.asyncio
    async def test_count_by_case_multiple(self, repository):
        """Test counting evidence for case with multiple items."""
        case_id = generate_case_id()

        # Create 5 evidence items
        for i in range(5):
            evidence = create_sample_evidence(case_id=case_id)
            await repository.create_evidence(evidence)

        count = await repository.count_by_case(case_id)

        assert count == 5

    @pytest.mark.asyncio
    async def test_count_excludes_other_cases(self, repository):
        """Test that count only includes evidence for specified case."""
        case_id_1 = generate_case_id()
        case_id_2 = generate_case_id()

        # Create evidence for case 1
        for i in range(3):
            evidence = create_sample_evidence(case_id=case_id_1)
            await repository.create_evidence(evidence)

        # Create evidence for case 2
        for i in range(2):
            evidence = create_sample_evidence(case_id=case_id_2)
            await repository.create_evidence(evidence)

        count1 = await repository.count_by_case(case_id_1)
        count2 = await repository.count_by_case(case_id_2)

        assert count1 == 3
        assert count2 == 2


class TestRepositoryClear:
    """Tests for repository clearing."""

    @pytest.mark.asyncio
    async def test_clear_removes_all_evidence(self, repository):
        """Test that clear removes all evidence."""
        # Create multiple evidence items
        for i in range(5):
            evidence = create_sample_evidence()
            await repository.create_evidence(evidence)

        # Clear repository
        repository.clear()

        # Verify all removed
        result, total = await repository.list_evidence_by_case("case_any")
        assert total == 0
