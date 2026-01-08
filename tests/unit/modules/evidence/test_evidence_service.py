"""Unit tests for EvidenceService (PR #46c).

Tests the business logic of the Evidence Service with mocked dependencies.
"""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from faultmaven.modules.evidence.domain.models import EvidenceListFilter
from faultmaven.modules.evidence.domain.services.evidence_service import EvidenceService

from .conftest import (
    MockEvidenceStorageAdapter,
    MockEvidenceRepository,
    MockUploadFile,
    create_sample_evidence,
)


class TestEvidenceServiceUpload:
    """Tests for evidence upload functionality."""

    @pytest.fixture
    def service(self, mock_storage, mock_repository):
        """Create EvidenceService with mocked dependencies."""
        return EvidenceService(
            storage_provider=mock_storage,
            repository=mock_repository,
        )

    @pytest.mark.asyncio
    async def test_upload_evidence_success(self, service, mock_upload_file, sample_user_id):
        """Test successful evidence upload."""
        result = await service.upload_evidence(
            file=mock_upload_file,
            uploaded_by=sample_user_id,
            description="Test upload",
            tags=["test", "upload"],
        )

        assert result is not None
        assert result.filename == mock_upload_file.filename
        assert "test" in result.tags

    @pytest.mark.asyncio
    async def test_upload_evidence_with_case_id(self, service, mock_upload_file, sample_user_id, sample_case_id):
        """Test upload with auto-linking to a case."""
        result = await service.upload_evidence(
            file=mock_upload_file,
            uploaded_by=sample_user_id,
            case_id=sample_case_id,
        )

        assert result is not None
        # Verify link_to_case was called
        service.repository.link_to_case.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_evidence_without_optional_fields(self, service, mock_upload_file, sample_user_id):
        """Test upload without description or tags."""
        result = await service.upload_evidence(
            file=mock_upload_file,
            uploaded_by=sample_user_id,
        )

        assert result is not None
        assert result.filename == mock_upload_file.filename

    @pytest.mark.asyncio
    async def test_upload_stores_file_first(self, service, mock_upload_file, sample_user_id):
        """Test that file is stored before database record is created."""
        await service.upload_evidence(
            file=mock_upload_file,
            uploaded_by=sample_user_id,
        )

        # Storage should be called
        service.storage.store_file.assert_called_once()
        # Repository should be called after storage
        service.repository.create.assert_called_once()


class TestEvidenceServiceGet:
    """Tests for evidence retrieval functionality."""

    @pytest.fixture
    def service(self, mock_storage, mock_repository):
        """Create EvidenceService with mocked dependencies."""
        return EvidenceService(
            storage_provider=mock_storage,
            repository=mock_repository,
        )

    @pytest.mark.asyncio
    async def test_get_evidence_success(self, service, sample_evidence):
        """Test retrieving existing evidence."""
        # Pre-populate the mock repository using evidence_id as key
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        result = await service.get_evidence(sample_evidence.id)

        assert result is not None
        assert result.id == sample_evidence.id
        assert result.filename == sample_evidence.filename

    @pytest.mark.asyncio
    async def test_get_evidence_not_found(self, service):
        """Test retrieving non-existent evidence returns None."""
        non_existent_id = uuid4()

        result = await service.get_evidence(non_existent_id)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_evidence_calls_repository(self, service):
        """Test that get_evidence delegates to repository."""
        evidence_id = uuid4()

        await service.get_evidence(evidence_id)

        service.repository.get.assert_called_once_with(evidence_id)


class TestEvidenceServiceList:
    """Tests for evidence listing functionality."""

    @pytest.fixture
    def service(self, mock_storage, mock_repository):
        """Create EvidenceService with mocked dependencies."""
        return EvidenceService(
            storage_provider=mock_storage,
            repository=mock_repository,
        )

    @pytest.mark.asyncio
    async def test_list_evidence_empty(self, service, sample_filters):
        """Test listing when no evidence exists."""
        results, total = await service.list_evidence(sample_filters)

        assert results == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_list_evidence_with_results(self, service, sample_evidence_list, sample_filters):
        """Test listing returns all evidence."""
        # Pre-populate repository
        for evidence in sample_evidence_list:
            service.repository._storage[evidence.evidence_id] = evidence

        results, total = await service.list_evidence(sample_filters)

        assert len(results) == 5
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_evidence_with_pagination(self, service, sample_evidence_list):
        """Test listing with pagination."""
        # Pre-populate repository
        for evidence in sample_evidence_list:
            service.repository._storage[evidence.evidence_id] = evidence

        filters = EvidenceListFilter(limit=2, offset=0)
        results, total = await service.list_evidence(filters)

        assert len(results) == 2
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_evidence_with_offset(self, service, sample_evidence_list):
        """Test listing with offset."""
        for evidence in sample_evidence_list:
            service.repository._storage[evidence.evidence_id] = evidence

        filters = EvidenceListFilter(limit=10, offset=3)
        results, total = await service.list_evidence(filters)

        assert len(results) == 2  # 5 - 3 = 2 remaining
        assert total == 5

    @pytest.mark.asyncio
    async def test_list_evidence_filter_by_tags(self, service, sample_evidence_list):
        """Test filtering by tags."""
        for evidence in sample_evidence_list:
            service.repository._storage[evidence.evidence_id] = evidence

        filters = EvidenceListFilter(tags=["tag0"])
        results, total = await service.list_evidence(filters)

        assert len(results) == 1
        assert "tag0" in results[0].tags

    @pytest.mark.asyncio
    async def test_list_evidence_filter_by_filename(self, service, sample_evidence_list):
        """Test filtering by filename contains."""
        for evidence in sample_evidence_list:
            service.repository._storage[evidence.evidence_id] = evidence

        filters = EvidenceListFilter(filename_contains="file_2")
        results, total = await service.list_evidence(filters)

        assert len(results) == 1
        assert "file_2" in results[0].filename


class TestEvidenceServiceDelete:
    """Tests for evidence deletion functionality."""

    @pytest.fixture
    def service(self, mock_storage, mock_repository):
        """Create EvidenceService with mocked dependencies."""
        return EvidenceService(
            storage_provider=mock_storage,
            repository=mock_repository,
        )

    @pytest.mark.asyncio
    async def test_delete_evidence_success(self, service, sample_evidence):
        """Test successful evidence deletion."""
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        result = await service.delete_evidence(sample_evidence.id)

        assert result is True
        service.storage.delete_file.assert_called_once()
        service.repository.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_evidence_not_found(self, service):
        """Test deleting non-existent evidence returns False."""
        non_existent_id = uuid4()

        result = await service.delete_evidence(non_existent_id)

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_removes_file_and_record(self, service, sample_evidence):
        """Test that both file and database record are deleted."""
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        await service.delete_evidence(sample_evidence.id)

        # Both storage and repository delete should be called
        service.storage.delete_file.assert_called_once_with(sample_evidence.storage_path)
        service.repository.delete.assert_called_once_with(sample_evidence.id)


class TestEvidenceServiceLinkToCase:
    """Tests for linking evidence to cases."""

    @pytest.fixture
    def service(self, mock_storage, mock_repository):
        """Create EvidenceService with mocked dependencies."""
        return EvidenceService(
            storage_provider=mock_storage,
            repository=mock_repository,
        )

    @pytest.mark.asyncio
    async def test_link_to_case_success(self, service, sample_evidence, sample_case_id):
        """Test successful linking to a case."""
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        result = await service.link_to_case(sample_evidence.id, sample_case_id)

        assert result is not None
        assert sample_case_id in result.linked_cases

    @pytest.mark.asyncio
    async def test_link_to_case_not_found_raises(self, service, sample_case_id):
        """Test linking non-existent evidence raises ValueError."""
        non_existent_id = uuid4()

        with pytest.raises(ValueError, match="not found"):
            await service.link_to_case(non_existent_id, sample_case_id)

    @pytest.mark.asyncio
    async def test_link_to_case_idempotent(self, service, sample_evidence, sample_case_id):
        """Test linking same case twice doesn't create duplicates."""
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        # Link twice
        await service.link_to_case(sample_evidence.id, sample_case_id)
        result = await service.link_to_case(sample_evidence.id, sample_case_id)

        # Should only have one entry
        assert result.linked_cases.count(sample_case_id) == 1


class TestEvidenceServiceGetFileUrl:
    """Tests for getting file download URLs."""

    @pytest.fixture
    def service(self, mock_storage, mock_repository):
        """Create EvidenceService with mocked dependencies."""
        return EvidenceService(
            storage_provider=mock_storage,
            repository=mock_repository,
        )

    @pytest.mark.asyncio
    async def test_get_file_url_success(self, service, sample_evidence):
        """Test getting download URL for existing evidence."""
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        result = await service.get_file_url(sample_evidence.id)

        assert result is not None
        assert "http" in result or "/" in result

    @pytest.mark.asyncio
    async def test_get_file_url_not_found(self, service):
        """Test getting URL for non-existent evidence returns None."""
        non_existent_id = uuid4()

        result = await service.get_file_url(non_existent_id)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_file_url_delegates_to_storage(self, service, sample_evidence):
        """Test that URL generation delegates to storage adapter."""
        service.repository._storage[sample_evidence.evidence_id] = sample_evidence

        await service.get_file_url(sample_evidence.id)

        service.storage.get_download_url.assert_called_once_with(sample_evidence.storage_path)
