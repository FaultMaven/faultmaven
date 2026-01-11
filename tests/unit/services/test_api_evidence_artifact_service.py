"""Unit tests for APIEvidenceArtifactService class (TASK-013).

Tests the API evidence artifact service layer functionality including:
- Upload evidence with validation
- Get evidence with authorization
- Download evidence file
- Update evidence metadata
- Delete evidence and file
- List evidence by case
- Set/get primary evidence
- Get evidence statistics
- Authorization checks for all operations
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
from faultmaven.services.file_storage_service import FileStorageService
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceListFilter,
    StorageBackend,
)
from faultmaven.modules.case.domain.models import Case, CaseStatus
from faultmaven.exceptions import (
    NotFoundError,
    AuthorizationError,
    ValidationException,
    ServiceError,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_case_repo():
    """Create mock case repository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_file_storage():
    """Create mock file storage service."""
    storage = AsyncMock(spec=FileStorageService)
    return storage


@pytest.fixture
def evidence_service(mock_case_repo, mock_file_storage):
    """Create APIEvidenceArtifactService with mocked dependencies."""
    return APIEvidenceArtifactService(
        case_repo=mock_case_repo,
        file_storage=mock_file_storage,
    )


@pytest.fixture
def sample_case():
    """Create sample case for testing."""
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_123",
        organization_id="org_456",
        title="Test Case",
        description="Test case description",
        status=CaseStatus.CONSULTING,
    )


@pytest.fixture
def sample_evidence():
    """Create sample evidence artifact for testing."""
    return EvidenceArtifact(
        evidence_id=f"evd_{uuid4().hex[:12]}",
        case_id="case_abc123",
        user_id="user_123",
        organization_id="org_456",
        original_filename="screenshot.png",
        stored_filename="abc123_screenshot.png",
        file_path="org_456/case_abc123/2024-01-15/abc123_screenshot.png",
        evidence_type=EvidenceArtifactType.SCREENSHOT,
        mime_type="image/png",
        file_size=102400,
        storage_backend=StorageBackend.LOCAL_FILESYSTEM,
        description="Error screenshot",
        is_primary=False,
    )


@pytest.fixture
def sample_file_data():
    """Create sample file data."""
    return b"PNG file data for testing purposes"


# ============================================================
# Upload Evidence Tests
# ============================================================

class TestUploadEvidence:
    """Test upload_evidence method."""

    @pytest.mark.asyncio
    async def test_upload_evidence_success(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test successful evidence upload."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc123_test.png",
            "file_path": "org_456/case_123/2024-01-15/abc123_test.png",
            "file_size": len(sample_file_data),
        }
        # Mock create_standalone_evidence to return evidence
        created_evidence = EvidenceArtifact(
            evidence_id="evd_abc123def456",
            case_id="standalone",  # Default until linked
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc123_test.png",
            file_path="org_456/case_123/2024-01-15/abc123_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
        )
        mock_case_repo.create_standalone_evidence.return_value = created_evidence
        # Mock link_standalone_evidence_to_case to return linked evidence
        linked_evidence = EvidenceArtifact(
            evidence_id=created_evidence.evidence_id,
            case_id=sample_case.case_id,
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc123_test.png",
            file_path="org_456/case_123/2024-01-15/abc123_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
        )
        mock_case_repo.link_standalone_evidence_to_case.return_value = linked_evidence

        result = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            file_data=sample_file_data,
            original_filename="test.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            description="Test screenshot",
        )

        assert result is not None
        mock_case_repo.get.assert_called_once_with(sample_case.case_id)
        mock_file_storage.store_file.assert_called_once()
        mock_case_repo.create_standalone_evidence.assert_called_once()
        mock_case_repo.link_standalone_evidence_to_case.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_evidence_generates_uuid_id(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test that upload_evidence generates UUID for evidence_id."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc_test.png",
            "file_path": "org/case/date/abc_test.png",
            "file_size": len(sample_file_data),
        }

        # Mock create_standalone_evidence to return evidence
        created_evidence = EvidenceArtifact(
            evidence_id="evd_test123",
            case_id="standalone",
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
        )
        mock_case_repo.create_standalone_evidence.return_value = created_evidence
        # Mock link_standalone_evidence_to_case to return linked evidence
        linked_evidence = EvidenceArtifact(
            evidence_id=created_evidence.evidence_id,
            case_id=sample_case.case_id,
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
        )
        mock_case_repo.link_standalone_evidence_to_case.return_value = linked_evidence

        result = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            file_data=sample_file_data,
            original_filename="test.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )

        assert result is not None
        assert result.evidence_id.startswith("evd_")

    @pytest.mark.asyncio
    async def test_upload_evidence_case_not_found_raises_not_found_error(
        self, evidence_service, mock_case_repo, sample_file_data
    ):
        """Test that missing case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError) as exc_info:
            await evidence_service.upload_evidence(
                case_id="nonexistent_case",
                organization_id="org_456",
                user_id="user_123",
                file_data=sample_file_data,
                original_filename="test.png",
                mime_type="image/png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
            )

        assert exc_info.value.resource_type == "Case"

    @pytest.mark.asyncio
    async def test_upload_evidence_wrong_org_raises_authorization_error(
        self, evidence_service, mock_case_repo, sample_case, sample_file_data
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id="different_org",
                user_id="user_123",
                file_data=sample_file_data,
                original_filename="test.png",
                mime_type="image/png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
            )

    @pytest.mark.asyncio
    async def test_upload_evidence_invalid_file_raises_validation_error(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case
    ):
        """Test that invalid file raises ValidationException."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.side_effect = ValidationException("file too large")

        with pytest.raises(ValidationException):
            await evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_123",
                file_data=b"x" * 1000,
                original_filename="test.png",
                mime_type="image/png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
            )

    @pytest.mark.asyncio
    async def test_upload_evidence_with_is_primary_sets_primary(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test that is_primary=True sets primary evidence."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc_test.png",
            "file_path": "org/case/date/abc_test.png",
            "file_size": len(sample_file_data),
        }
        mock_case_repo.create_standalone_evidence.side_effect = lambda e: e
        mock_case_repo.set_primary_evidence.return_value = True
        mock_case_repo.get_standalone_evidence.side_effect = lambda id: EvidenceArtifact(
            evidence_id=id,
            case_id=sample_case.case_id,
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=True,
        )

        result = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            file_data=sample_file_data,
            original_filename="test.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            is_primary=True,
        )

        mock_case_repo.set_primary_evidence.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_evidence_with_metadata(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test uploading evidence with metadata."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc_test.png",
            "file_path": "org/case/date/abc_test.png",
            "file_size": len(sample_file_data),
        }

        # Mock create_standalone_evidence to return evidence with metadata
        created_evidence = EvidenceArtifact(
            evidence_id="evd_meta123",
            case_id="standalone",
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
            metadata={"resolution": "1920x1080", "source": "browser"},
        )
        mock_case_repo.create_standalone_evidence.return_value = created_evidence
        # Mock link_standalone_evidence_to_case to return linked evidence
        linked_evidence = EvidenceArtifact(
            evidence_id=created_evidence.evidence_id,
            case_id=sample_case.case_id,
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
            metadata={"resolution": "1920x1080", "source": "browser"},
        )
        mock_case_repo.link_standalone_evidence_to_case.return_value = linked_evidence

        metadata = {"resolution": "1920x1080", "source": "browser"}

        result = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            file_data=sample_file_data,
            original_filename="test.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            metadata=metadata,
        )

        assert result.metadata == metadata


# ============================================================
# Get Evidence Tests
# ============================================================

class TestGetEvidence:
    """Test get_evidence method."""

    @pytest.mark.asyncio
    async def test_get_evidence_success(
        self, evidence_service, sample_evidence
    ):
        """Test successful evidence retrieval."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        result = await evidence_service.get_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
        )

        assert result is not None
        assert result.evidence_id == sample_evidence.evidence_id
        mock_case_repo.get_standalone_evidence.assert_called_once_with(sample_evidence.evidence_id)

    @pytest.mark.asyncio
    async def test_get_evidence_returns_none_if_not_found(
        self, evidence_service):
        """Test that missing evidence returns None."""
        mock_case_repo.get_standalone_evidence.return_value = None

        result = await evidence_service.get_evidence(
            evidence_id="nonexistent",
            organization_id="org_456",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_evidence_wrong_org_returns_none(
        self, evidence_service, sample_evidence
    ):
        """Test that wrong organization returns None."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        result = await evidence_service.get_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id="different_org",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_evidence_empty_id_returns_none(
        self, evidence_service
    ):
        """Test that empty evidence_id returns None."""
        result = await evidence_service.get_evidence(
            evidence_id="",
            organization_id="org_456",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_evidence_whitespace_id_returns_none(
        self, evidence_service
    ):
        """Test that whitespace-only evidence_id returns None."""
        result = await evidence_service.get_evidence(
            evidence_id="   ",
            organization_id="org_456",
        )

        assert result is None


# ============================================================
# Download Evidence Tests
# ============================================================

class TestDownloadEvidence:
    """Test download_evidence method."""

    @pytest.mark.asyncio
    async def test_download_evidence_success(
        self, evidence_service, mock_file_storage, sample_evidence, sample_file_data
    ):
        """Test successful evidence download."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_file_storage.retrieve_file.return_value = sample_file_data

        data, filename, mime_type = await evidence_service.download_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
        )

        assert data == sample_file_data
        assert filename == sample_evidence.original_filename
        assert mime_type == sample_evidence.mime_type
        mock_file_storage.retrieve_file.assert_called_once_with(sample_evidence.file_path)

    @pytest.mark.asyncio
    async def test_download_evidence_not_found_raises_not_found_error(
        self, evidence_service):
        """Test that missing evidence raises NotFoundError."""
        mock_case_repo.get_standalone_evidence.return_value = None

        with pytest.raises(NotFoundError):
            await evidence_service.download_evidence(
                evidence_id="nonexistent",
                organization_id="org_456",
            )

    @pytest.mark.asyncio
    async def test_download_evidence_wrong_org_raises_authorization_error(
        self, evidence_service, sample_evidence
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        with pytest.raises(AuthorizationError):
            await evidence_service.download_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id="different_org",
            )

    @pytest.mark.asyncio
    async def test_download_evidence_file_missing_raises_service_error(
        self, evidence_service, mock_file_storage, sample_evidence
    ):
        """Test that missing file raises ServiceError."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_file_storage.retrieve_file.side_effect = Exception("File not found")

        with pytest.raises(ServiceError):
            await evidence_service.download_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id=sample_evidence.organization_id,
            )


# ============================================================
# Update Evidence Tests
# ============================================================

class TestUpdateEvidence:
    """Test update_evidence method."""

    @pytest.mark.asyncio
    async def test_update_evidence_description_success(
        self, evidence_service, sample_evidence
    ):
        """Test successful description update."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        updated_evidence = EvidenceArtifact(**{
            **sample_evidence.__dict__,
            "description": "Updated description",
        })
        mock_case_repo.update_standalone_evidence.return_value = updated_evidence

        result = await evidence_service.update_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
            updates={"description": "Updated description"},
        )

        assert result.description == "Updated description"
        mock_case_repo.update_standalone_evidence.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_evidence_metadata_success(
        self, evidence_service, sample_evidence
    ):
        """Test successful metadata update."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_case_repo.update_standalone_evidence.side_effect = lambda e: e

        new_metadata = {"key": "value"}

        result = await evidence_service.update_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
            updates={"metadata": new_metadata},
        )

        assert result.metadata == new_metadata

    @pytest.mark.asyncio
    async def test_update_evidence_is_primary_true(
        self, evidence_service, sample_evidence
    ):
        """Test setting is_primary to True."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_case_repo.set_primary_evidence.return_value = True

        primary_evidence = EvidenceArtifact(**{
            **sample_evidence.__dict__,
            "is_primary": True,
        })
        mock_case_repo.get_standalone_evidence.side_effect = [sample_evidence, primary_evidence]

        result = await evidence_service.update_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
            updates={"is_primary": True},
        )

        mock_case_repo.set_primary_evidence.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_evidence_not_found_raises_not_found_error(
        self, evidence_service):
        """Test that missing evidence raises NotFoundError."""
        mock_case_repo.get_standalone_evidence.return_value = None

        with pytest.raises(NotFoundError):
            await evidence_service.update_evidence(
                evidence_id="nonexistent",
                organization_id="org_456",
                updates={"description": "test"},
            )

    @pytest.mark.asyncio
    async def test_update_evidence_wrong_org_raises_authorization_error(
        self, evidence_service, sample_evidence
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        with pytest.raises(AuthorizationError):
            await evidence_service.update_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id="different_org",
                updates={"description": "test"},
            )

    @pytest.mark.asyncio
    async def test_update_evidence_empty_updates_raises_validation_error(
        self, evidence_service
    ):
        """Test that empty updates raises ValidationException."""
        with pytest.raises(ValidationException):
            await evidence_service.update_evidence(
                evidence_id="evd_123",
                organization_id="org_456",
                updates={},
            )

    @pytest.mark.asyncio
    async def test_update_evidence_invalid_metadata_raises_validation_error(
        self, evidence_service, sample_evidence
    ):
        """Test that non-dict metadata raises ValidationException."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        with pytest.raises(ValidationException):
            await evidence_service.update_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id=sample_evidence.organization_id,
                updates={"metadata": "not a dict"},
            )

    @pytest.mark.asyncio
    async def test_update_evidence_no_valid_fields_raises_validation_error(
        self, evidence_service, sample_evidence
    ):
        """Test that updates with only invalid fields raises ValidationException."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        with pytest.raises(ValidationException):
            await evidence_service.update_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id=sample_evidence.organization_id,
                updates={"file_content": "cannot update this"},
            )


# ============================================================
# Delete Evidence Tests
# ============================================================

class TestDeleteEvidence:
    """Test delete_evidence method."""

    @pytest.mark.asyncio
    async def test_delete_evidence_success(
        self, evidence_service, mock_file_storage, sample_evidence
    ):
        """Test successful evidence deletion."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_file_storage.delete_file.return_value = True
        mock_case_repo.delete_standalone_evidence.return_value = True

        result = await evidence_service.delete_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
        )

        assert result is True
        mock_file_storage.delete_file.assert_called_once_with(sample_evidence.file_path)
        mock_case_repo.delete_standalone_evidence.assert_called_once_with(sample_evidence.evidence_id)

    @pytest.mark.asyncio
    async def test_delete_evidence_not_found_returns_false(
        self, evidence_service):
        """Test that missing evidence returns False."""
        mock_case_repo.get_standalone_evidence.return_value = None

        result = await evidence_service.delete_evidence(
            evidence_id="nonexistent",
            organization_id="org_456",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_delete_evidence_wrong_org_raises_authorization_error(
        self, evidence_service, sample_evidence
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        with pytest.raises(AuthorizationError):
            await evidence_service.delete_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id="different_org",
            )

    @pytest.mark.asyncio
    async def test_delete_evidence_file_already_missing(
        self, evidence_service, mock_file_storage, sample_evidence
    ):
        """Test that missing file doesn't prevent deletion."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_file_storage.delete_file.return_value = False  # File not found
        mock_case_repo.delete_standalone_evidence.return_value = True

        result = await evidence_service.delete_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
        )

        assert result is True  # Record should still be deleted

    @pytest.mark.asyncio
    async def test_delete_evidence_empty_id_returns_false(
        self, evidence_service
    ):
        """Test that empty evidence_id returns False."""
        result = await evidence_service.delete_evidence(
            evidence_id="",
            organization_id="org_456",
        )

        assert result is False


# ============================================================
# List Evidence by Case Tests
# ============================================================

class TestListEvidenceByCase:
    """Test list_evidence_by_case method."""

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_success(
        self, evidence_service, mock_case_repo, sample_case, sample_evidence
    ):
        """Test successful listing of evidence."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_standalone_evidence.return_value = ([sample_evidence], 1)

        result = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert len(result) == 1
        assert result[0].evidence_id == sample_evidence.evidence_id

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_with_type_filter(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test listing with evidence type filter."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_standalone_evidence.return_value = ([], 0)

        await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )

        mock_case_repo.list_standalone_evidence.assert_called_once()
        call_args = mock_case_repo.list_standalone_evidence.call_args
        assert call_args is not None
        filters = call_args[0][0]  # First positional argument
        assert isinstance(filters, EvidenceListFilter)
        assert filters.case_id == sample_case.case_id
        assert filters.limit == 50
        assert filters.offset == 0

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_with_pagination(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test listing with pagination."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_standalone_evidence.return_value = ([], 0)

        await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            limit=20,
            offset=10,
        )

        mock_case_repo.list_standalone_evidence.assert_called_once()
        call_args = mock_case_repo.list_standalone_evidence.call_args
        assert call_args is not None
        filters = call_args[0][0]  # First positional argument
        assert isinstance(filters, EvidenceListFilter)
        assert filters.case_id == sample_case.case_id
        assert filters.limit == 20
        assert filters.offset == 10

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_not_found_raises_not_found_error(
        self, evidence_service, mock_case_repo
    ):
        """Test that missing case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await evidence_service.list_evidence_by_case(
                case_id="nonexistent",
                organization_id="org_456",
            )

    @pytest.mark.asyncio
    async def test_list_evidence_by_case_wrong_org_raises_authorization_error(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await evidence_service.list_evidence_by_case(
                case_id=sample_case.case_id,
                organization_id="different_org",
            )


# ============================================================
# Set Primary Evidence Tests
# ============================================================

class TestSetPrimaryEvidence:
    """Test set_primary_evidence method."""

    @pytest.mark.asyncio
    async def test_set_primary_evidence_success(
        self, evidence_service, sample_evidence
    ):
        """Test successful setting of primary evidence."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence
        mock_case_repo.set_primary_evidence.return_value = True

        primary_evidence = EvidenceArtifact(**{
            **sample_evidence.__dict__,
            "is_primary": True,
        })
        mock_case_repo.get_standalone_evidence.side_effect = [sample_evidence, primary_evidence]

        result = await evidence_service.set_primary_evidence(
            evidence_id=sample_evidence.evidence_id,
            organization_id=sample_evidence.organization_id,
        )

        assert result.is_primary is True
        mock_case_repo.set_primary_evidence.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_primary_evidence_not_found_raises_not_found_error(
        self, evidence_service):
        """Test that missing evidence raises NotFoundError."""
        mock_case_repo.get_standalone_evidence.return_value = None

        with pytest.raises(NotFoundError):
            await evidence_service.set_primary_evidence(
                evidence_id="nonexistent",
                organization_id="org_456",
            )

    @pytest.mark.asyncio
    async def test_set_primary_evidence_wrong_org_raises_authorization_error(
        self, evidence_service, sample_evidence
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get_standalone_evidence.return_value = sample_evidence

        with pytest.raises(AuthorizationError):
            await evidence_service.set_primary_evidence(
                evidence_id=sample_evidence.evidence_id,
                organization_id="different_org",
            )


# ============================================================
# Get Primary Evidence Tests
# ============================================================

class TestGetPrimaryEvidence:
    """Test get_primary_evidence method."""

    @pytest.mark.asyncio
    async def test_get_primary_evidence_success(
        self, evidence_service, mock_case_repo, sample_case, sample_evidence
    ):
        """Test successful retrieval of primary evidence."""
        mock_case_repo.get.return_value = sample_case
        primary_evidence = EvidenceArtifact(**{
            **sample_evidence.__dict__,
            "case_id": sample_case.case_id,
            "is_primary": True,
        })
        mock_case_repo.get_primary_evidence.return_value = primary_evidence

        result = await evidence_service.get_primary_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert result is not None
        assert result.is_primary is True

    @pytest.mark.asyncio
    async def test_get_primary_evidence_not_set_returns_none(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test that no primary evidence returns None."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.get_primary_evidence.return_value = None

        result = await evidence_service.get_primary_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_primary_evidence_case_not_found_raises_not_found_error(
        self, evidence_service, mock_case_repo
    ):
        """Test that missing case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await evidence_service.get_primary_evidence(
                case_id="nonexistent",
                organization_id="org_456",
            )

    @pytest.mark.asyncio
    async def test_get_primary_evidence_wrong_org_raises_authorization_error(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test that wrong organization raises AuthorizationError."""
        mock_case_repo.get.return_value = sample_case

        with pytest.raises(AuthorizationError):
            await evidence_service.get_primary_evidence(
                case_id=sample_case.case_id,
                organization_id="different_org",
            )


# ============================================================
# Get Evidence Statistics Tests
# ============================================================

class TestGetEvidenceStatistics:
    """Test get_evidence_statistics method."""

    @pytest.mark.asyncio
    async def test_get_evidence_statistics_success(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test successful statistics retrieval."""
        mock_case_repo.get.return_value = sample_case

        # Create evidence list with different types
        evidence_list = [
            EvidenceArtifact(
                evidence_id="evd_1",
                case_id=sample_case.case_id,
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="screenshot.png",
                stored_filename="abc_screenshot.png",
                file_path="path/to/file",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="image/png",
                file_size=102400,
                is_primary=True,
            ),
            EvidenceArtifact(
                evidence_id="evd_2",
                case_id=sample_case.case_id,
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="log.txt",
                stored_filename="abc_log.txt",
                file_path="path/to/log",
                evidence_type=EvidenceArtifactType.LOG_FILE,
                mime_type="text/plain",
                file_size=51200,
                is_primary=False,
            ),
            EvidenceArtifact(
                evidence_id="evd_3",
                case_id=sample_case.case_id,
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="trace.pcap",
                stored_filename="abc_trace.pcap",
                file_path="path/to/trace",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
                mime_type="application/octet-stream",
                file_size=204800,
                is_primary=False,
            ),
        ]
        mock_case_repo.list_standalone_evidence.return_value = (evidence_list, 3)

        result = await evidence_service.get_evidence_statistics(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert result["total_artifacts"] == 3
        assert result["by_type"]["screenshot"] == 2
        assert result["by_type"]["log_file"] == 1
        assert result["total_file_size_bytes"] == 102400 + 51200 + 204800
        assert result["primary_evidence_id"] == "evd_1"

    @pytest.mark.asyncio
    async def test_get_evidence_statistics_empty_case(
        self, evidence_service, mock_case_repo, sample_case
    ):
        """Test statistics for case with no evidence."""
        mock_case_repo.get.return_value = sample_case
        mock_case_repo.list_standalone_evidence.return_value = ([], 0)

        result = await evidence_service.get_evidence_statistics(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert result["total_artifacts"] == 0
        assert result["by_type"] == {}
        assert result["total_file_size_bytes"] == 0
        assert result["primary_evidence_id"] is None

    @pytest.mark.asyncio
    async def test_get_evidence_statistics_case_not_found_raises_not_found_error(
        self, evidence_service, mock_case_repo
    ):
        """Test that missing case raises NotFoundError."""
        mock_case_repo.get.return_value = None

        with pytest.raises(NotFoundError):
            await evidence_service.get_evidence_statistics(
                case_id="nonexistent",
                organization_id="org_456",
            )


# ============================================================
# Delete All Evidence For Case Tests
# ============================================================

class TestDeleteAllEvidenceForCase:
    """Test delete_all_evidence_for_case method."""

    @pytest.mark.asyncio
    async def test_delete_all_evidence_for_case_success(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case
    ):
        """Test successful deletion of all evidence for a case."""
        mock_case_repo.get.return_value = sample_case

        evidence_list = [
            EvidenceArtifact(
                evidence_id="evd_1",
                case_id=sample_case.case_id,
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="file1.txt",
                stored_filename="abc_file1.txt",
                file_path="path/to/file1",
                evidence_type=EvidenceArtifactType.LOG_FILE,
                mime_type="text/plain",
                file_size=1024,
            ),
            EvidenceArtifact(
                evidence_id="evd_2",
                case_id=sample_case.case_id,
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="file2.txt",
                stored_filename="abc_file2.txt",
                file_path="path/to/file2",
                evidence_type=EvidenceArtifactType.LOG_FILE,
                mime_type="text/plain",
                file_size=2048,
            ),
        ]
        mock_case_repo.list_standalone_evidence.return_value = (evidence_list, 2)
        mock_file_storage.delete_file.return_value = True
        mock_case_repo.delete_standalone_evidence.return_value = True

        result = await evidence_service.delete_all_evidence_for_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert result == 2
        assert mock_file_storage.delete_file.call_count == 2
        assert mock_case_repo.delete_standalone_evidence.call_count == 2


# ============================================================
# Health Check Tests
# ============================================================

class TestHealthCheck:
    """Test health_check method."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(
        self, evidence_service, mock_file_storage
    ):
        """Test health check returns healthy status."""
        mock_file_storage.health_check.return_value = {
            "status": "healthy",
            "storage_accessible": True,
        }

        result = await evidence_service.health_check()

        assert result["status"] == "healthy"
        assert "file_storage" in result

    @pytest.mark.asyncio
    async def test_health_check_degraded_when_storage_unhealthy(
        self, evidence_service, mock_file_storage
    ):
        """Test health check returns degraded when storage is unhealthy."""
        mock_file_storage.health_check.return_value = {
            "status": "unhealthy",
            "error": "Storage not accessible",
        }

        result = await evidence_service.health_check()

        assert result["status"] == "degraded"


# ============================================================
# Edge Cases Tests
# ============================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_upload_evidence_with_very_long_description(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test uploading evidence with very long description."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc_test.png",
            "file_path": "org/case/date/abc_test.png",
            "file_size": len(sample_file_data),
        }
        long_description = "A" * 10000
        # Mock create_standalone_evidence to return evidence
        created_evidence = EvidenceArtifact(
            evidence_id="evd_long123",
            case_id="standalone",
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
            description=long_description,
        )
        mock_case_repo.create_standalone_evidence.return_value = created_evidence
        # Mock link_standalone_evidence_to_case to return linked evidence
        linked_evidence = EvidenceArtifact(
            evidence_id=created_evidence.evidence_id,
            case_id=sample_case.case_id,
            user_id="user_123",
            organization_id=sample_case.organization_id,
            original_filename="test.png",
            stored_filename="abc_test.png",
            file_path="org/case/date/abc_test.png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            mime_type="image/png",
            file_size=len(sample_file_data),
            is_primary=False,
            description=long_description,
        )
        mock_case_repo.link_standalone_evidence_to_case.return_value = linked_evidence

        result = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_123",
            file_data=sample_file_data,
            original_filename="test.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            description=long_description,
        )

        assert result.description == long_description

    @pytest.mark.asyncio
    async def test_upload_evidence_all_evidence_types(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test uploading evidence with all evidence types."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc_test.bin",
            "file_path": "org/case/date/abc_test.bin",
            "file_size": len(sample_file_data),
        }

        for evidence_type in EvidenceArtifactType:
            # Mock create_standalone_evidence to return evidence with current type
            created_evidence = EvidenceArtifact(
                evidence_id=f"evd_{evidence_type.value}123",
                case_id="standalone",
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="test.bin",
                stored_filename="abc_test.bin",
                file_path="org/case/date/abc_test.bin",
                evidence_type=evidence_type,
                mime_type="application/octet-stream",
                file_size=len(sample_file_data),
                is_primary=False,
            )
            mock_case_repo.create_standalone_evidence.return_value = created_evidence
            # Mock link_standalone_evidence_to_case to return linked evidence
            linked_evidence = EvidenceArtifact(
                evidence_id=created_evidence.evidence_id,
                case_id=sample_case.case_id,
                user_id="user_123",
                organization_id=sample_case.organization_id,
                original_filename="test.bin",
                stored_filename="abc_test.bin",
                file_path="org/case/date/abc_test.bin",
                evidence_type=evidence_type,
                mime_type="application/octet-stream",
                file_size=len(sample_file_data),
                is_primary=False,
            )
            mock_case_repo.link_standalone_evidence_to_case.return_value = linked_evidence

            result = await evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_123",
                file_data=sample_file_data,
                original_filename="test.bin",
                mime_type="application/octet-stream",
                evidence_type=evidence_type,
            )

            assert result.evidence_type == evidence_type

    @pytest.mark.asyncio
    async def test_concurrent_uploads_generate_unique_ids(
        self, evidence_service, mock_case_repo, mock_file_storage, sample_case, sample_file_data
    ):
        """Test that concurrent uploads generate unique IDs."""
        mock_case_repo.get.return_value = sample_case
        mock_file_storage.store_file.return_value = {
            "stored_filename": "abc_test.png",
            "file_path": "org/case/date/abc_test.png",
            "file_size": len(sample_file_data),
        }

        created_ids = []

        async def capture_id(e):
            created_ids.append(e.evidence_id)
            return e

        mock_case_repo.create_standalone_evidence.side_effect = capture_id

        import asyncio
        await asyncio.gather(*[
            evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_123",
                file_data=sample_file_data,
                original_filename=f"test{i}.png",
                mime_type="image/png",
                evidence_type=EvidenceArtifactType.SCREENSHOT,
            )
            for i in range(5)
        ])

        # All IDs should be unique
        assert len(set(created_ids)) == 5
