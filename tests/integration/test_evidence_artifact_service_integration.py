"""Integration tests for Evidence Artifact Service Layer (TASK-013).

Tests the full evidence artifact service functionality with real file
storage and database operations.

Run with:
    pytest tests/integration/test_evidence_artifact_service_integration.py -v

Requirements:
    - SQLite for local testing (automatic)
    - Temporary file storage directory (automatic cleanup)
"""

import os
import tempfile
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
)
from faultmaven.services.file_storage_service import FileStorageService
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
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
from faultmaven.exceptions import (
    NotFoundError,
    AuthorizationError,
    ValidationException,
    ServiceError,
)
from tests.utils import generate_case_id, generate_evidence_id


# ============================================================
# Helper Functions
# ============================================================


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
def temp_storage_dir():
    """Create temporary directory for file storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture(scope="function")
async def case_repository(test_session) -> DatabaseCaseRepository:
    """Create DatabaseCaseRepository with test session."""
    return DatabaseCaseRepository(test_session)


@pytest.fixture(scope="function")
async def evidence_repository(test_session) -> DatabaseEvidenceArtifactRepository:
    """Create DatabaseEvidenceArtifactRepository with test session."""
    return DatabaseEvidenceArtifactRepository(test_session)


@pytest.fixture(scope="function")
def file_storage_service(temp_storage_dir) -> FileStorageService:
    """Create FileStorageService with temp directory."""
    return FileStorageService(
        storage_root=temp_storage_dir,
        max_file_size_bytes=10 * 1024 * 1024,  # 10MB
        allowed_mime_types=[],  # Allow all
    )


@pytest.fixture(scope="function")
async def evidence_service(
    case_repository,
    file_storage_service,
) -> APIEvidenceArtifactService:
    """Create APIEvidenceArtifactService with test dependencies."""
    return APIEvidenceArtifactService(
        case_repo=case_repository,
        file_storage=file_storage_service,
    )


@pytest.fixture
async def sample_case(case_repository) -> Case:
    """Create and save a sample case."""
    case = Case(
        case_id=generate_case_id(),
        user_id="user_integration_test",
        organization_id="org_integration_test",
        title="Integration Test Case",
        description="Case for testing evidence service integration",
        status=CaseStatus.CONSULTING,
        investigation_strategy=InvestigationStrategy.POST_MORTEM,
    )
    await case_repository.save(case)
    return case


@pytest.fixture
def sample_file_data() -> bytes:
    """Create sample file data."""
    return b"Integration test file content - " * 100


@pytest.fixture
def sample_image_data() -> bytes:
    """Create sample PNG image data (minimal valid PNG)."""
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


# ============================================================
# End-to-End Upload/Download Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestUploadDownloadWorkflow:
    """Test complete upload/download workflow."""

    @pytest.mark.asyncio
    async def test_upload_and_download_evidence(
        self, evidence_service, sample_case, sample_file_data, temp_storage_dir
    ):
        """Test complete upload/download workflow."""
        # Upload evidence
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test_document.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            description="Test log file",
        )

        assert uploaded is not None
        assert uploaded.evidence_id.startswith("evd_")

        # Verify file stored on disk
        full_path = os.path.join(temp_storage_dir, uploaded.file_path)
        assert os.path.exists(full_path)

        # Verify file size matches
        assert os.path.getsize(full_path) == len(sample_file_data)

        # Download evidence
        data, filename, mime_type = await evidence_service.download_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
        )

        # Verify downloaded data matches uploaded data
        assert data == sample_file_data
        assert filename == "test_document.txt"
        assert mime_type == "text/plain"

    @pytest.mark.asyncio
    async def test_upload_image_and_download(
        self, evidence_service, sample_case, sample_image_data
    ):
        """Test uploading and downloading binary image data."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_image_data,
            original_filename="screenshot.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )

        assert uploaded.evidence_type == EvidenceArtifactType.SCREENSHOT

        data, filename, mime_type = await evidence_service.download_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
        )

        assert data == sample_image_data
        assert filename == "screenshot.png"
        assert mime_type == "image/png"


# ============================================================
# Authorization Enforcement Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestAuthorizationEnforcement:
    """Test organization-level authorization."""

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_get(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test that get_evidence returns None for wrong organization."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        # Try to access with different organization
        result = await evidence_service.get_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id="different_org",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_download(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test that download fails for wrong organization."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        with pytest.raises(AuthorizationError):
            await evidence_service.download_evidence(
                evidence_id=uploaded.evidence_id,
                organization_id="different_org",
            )

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_delete(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test that delete fails for wrong organization."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        with pytest.raises(AuthorizationError):
            await evidence_service.delete_evidence(
                evidence_id=uploaded.evidence_id,
                organization_id="different_org",
            )

    @pytest.mark.asyncio
    async def test_authorization_prevents_cross_org_list(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test that list fails for wrong organization."""
        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        with pytest.raises(AuthorizationError):
            await evidence_service.list_evidence_by_case(
                case_id=sample_case.case_id,
                organization_id="different_org",
            )


# ============================================================
# Primary Evidence Management Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestPrimaryEvidenceManagement:
    """Test primary evidence management."""

    @pytest.mark.asyncio
    async def test_set_primary_evidence(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test setting primary evidence."""
        # Upload evidence without is_primary
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        # Set as primary
        primary = await evidence_service.set_primary_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
        )

        assert primary.is_primary is True

        # Get primary evidence
        fetched_primary = await evidence_service.get_primary_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert fetched_primary is not None
        assert fetched_primary.evidence_id == uploaded.evidence_id

    @pytest.mark.asyncio
    async def test_new_primary_unsets_previous(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test that setting new primary unsets previous."""
        # Upload first evidence as primary
        first = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="first.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            is_primary=True,
        )

        # Upload second evidence as primary
        second = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="second.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            is_primary=True,
        )

        # Verify second is now primary
        primary = await evidence_service.get_primary_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert primary is not None
        assert primary.evidence_id == second.evidence_id

        # Verify first is no longer primary
        first_updated = await evidence_service.get_evidence(
            evidence_id=first.evidence_id,
            organization_id=sample_case.organization_id,
        )
        assert first_updated.is_primary is False

    @pytest.mark.asyncio
    async def test_get_primary_when_none_set(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test getting primary evidence when none is set."""
        # Upload evidence without setting primary
        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            is_primary=False,
        )

        primary = await evidence_service.get_primary_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert primary is None


# ============================================================
# Delete and Cleanup Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestDeleteAndCleanup:
    """Test evidence deletion and cleanup."""

    @pytest.mark.asyncio
    async def test_delete_removes_file_and_record(
        self, evidence_service, sample_case, sample_file_data, temp_storage_dir
    ):
        """Test that delete removes both file and database record."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="to_delete.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        file_path = os.path.join(temp_storage_dir, uploaded.file_path)
        assert os.path.exists(file_path)

        # Delete evidence
        deleted = await evidence_service.delete_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
        )

        assert deleted is True

        # Verify file is removed
        assert not os.path.exists(file_path)

        # Verify record is removed
        fetched = await evidence_service.get_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
        )
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_all_evidence_for_case(
        self, evidence_service, sample_case, sample_file_data, temp_storage_dir
    ):
        """Test deleting all evidence for a case."""
        # Upload multiple evidence files
        evidence_ids = []
        for i in range(5):
            uploaded = await evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_integration_test",
                file_data=sample_file_data,
                original_filename=f"file_{i}.txt",
                mime_type="text/plain",
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )
            evidence_ids.append(uploaded.evidence_id)

        # Delete all
        deleted_count = await evidence_service.delete_all_evidence_for_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert deleted_count == 5

        # Verify all records are removed
        evidence_list = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )
        assert len(evidence_list) == 0


# ============================================================
# Multiple Evidence Types Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestMultipleEvidenceTypes:
    """Test handling multiple evidence types."""

    @pytest.mark.asyncio
    async def test_upload_multiple_evidence_types(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test uploading different evidence types."""
        types_to_test = [
            (EvidenceArtifactType.SCREENSHOT, "screenshot.png", "image/png"),
            (EvidenceArtifactType.LOG_FILE, "app.log", "text/plain"),
            (EvidenceArtifactType.NETWORK_TRACE, "trace.har", "application/json"),
            (EvidenceArtifactType.CONFIGURATION, "config.yaml", "application/yaml"),
            (EvidenceArtifactType.CODE_SNIPPET, "snippet.py", "text/x-python"),
        ]

        uploaded_evidence = []

        for evidence_type, filename, mime_type in types_to_test:
            uploaded = await evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_integration_test",
                file_data=sample_file_data,
                original_filename=filename,
                mime_type=mime_type,
                evidence_type=evidence_type,
            )
            uploaded_evidence.append(uploaded)

        # Verify all were uploaded
        all_evidence = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )
        assert len(all_evidence) == 5

        # Verify each type is correct
        for uploaded in uploaded_evidence:
            fetched = await evidence_service.get_evidence(
                evidence_id=uploaded.evidence_id,
                organization_id=sample_case.organization_id,
            )
            assert fetched.evidence_type == uploaded.evidence_type

    @pytest.mark.asyncio
    async def test_filter_by_evidence_type(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test filtering evidence by type."""
        # Upload different types
        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="screenshot.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )

        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="app.log",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="screenshot2.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )

        # Filter by SCREENSHOT
        screenshots = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            evidence_type=EvidenceArtifactType.SCREENSHOT,
        )
        assert len(screenshots) == 2

        # Filter by LOG_FILE
        logs = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )
        assert len(logs) == 1


# ============================================================
# Statistics Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestEvidenceStatistics:
    """Test evidence statistics calculation."""

    @pytest.mark.asyncio
    async def test_get_statistics(
        self, evidence_service, sample_case, sample_file_data, sample_image_data
    ):
        """Test getting evidence statistics."""
        # Upload various evidence
        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="log1.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="log2.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        primary = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_image_data,
            original_filename="screenshot.png",
            mime_type="image/png",
            evidence_type=EvidenceArtifactType.SCREENSHOT,
            is_primary=True,
        )

        # Get statistics
        stats = await evidence_service.get_evidence_statistics(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
        )

        assert stats["total_artifacts"] == 3
        assert stats["by_type"]["log_file"] == 2
        assert stats["by_type"]["screenshot"] == 1
        assert stats["total_file_size_bytes"] == (len(sample_file_data) * 2) + len(
            sample_image_data
        )
        assert stats["primary_evidence_id"] == primary.evidence_id


# ============================================================
# Pagination Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestPagination:
    """Test evidence listing pagination."""

    @pytest.mark.asyncio
    async def test_list_with_pagination(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test listing evidence with pagination."""
        # Upload 15 evidence files
        for i in range(15):
            await evidence_service.upload_evidence(
                case_id=sample_case.case_id,
                organization_id=sample_case.organization_id,
                user_id="user_integration_test",
                file_data=sample_file_data,
                original_filename=f"file_{i:02d}.txt",
                mime_type="text/plain",
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )

        # Get first page
        page1 = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            limit=5,
            offset=0,
        )
        assert len(page1) == 5

        # Get second page
        page2 = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            limit=5,
            offset=5,
        )
        assert len(page2) == 5

        # Get third page
        page3 = await evidence_service.list_evidence_by_case(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            limit=5,
            offset=10,
        )
        assert len(page3) == 5

        # Verify no overlap
        all_ids = (
            [e.evidence_id for e in page1]
            + [e.evidence_id for e in page2]
            + [e.evidence_id for e in page3]
        )
        assert len(set(all_ids)) == 15


# ============================================================
# Update Evidence Tests
# ============================================================


@pytest.mark.skip(
    reason="Requires unimplemented standalone evidence methods in DatabaseCaseRepository "
    "(create_standalone_evidence, get_standalone_evidence, link_standalone_evidence_to_case). "
    "See: faultmaven/modules/case/infrastructure/postgresql_hybrid_case_repository.py:1415-1435"
)
class TestUpdateEvidence:
    """Test evidence update operations."""

    @pytest.mark.asyncio
    async def test_update_description(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test updating evidence description."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
            description="Original description",
        )

        updated = await evidence_service.update_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
            updates={"description": "Updated description"},
        )

        assert updated.description == "Updated description"

    @pytest.mark.asyncio
    async def test_update_metadata(
        self, evidence_service, sample_case, sample_file_data
    ):
        """Test updating evidence metadata."""
        uploaded = await evidence_service.upload_evidence(
            case_id=sample_case.case_id,
            organization_id=sample_case.organization_id,
            user_id="user_integration_test",
            file_data=sample_file_data,
            original_filename="test.txt",
            mime_type="text/plain",
            evidence_type=EvidenceArtifactType.LOG_FILE,
        )

        new_metadata = {"source": "browser", "version": "1.0"}

        updated = await evidence_service.update_evidence(
            evidence_id=uploaded.evidence_id,
            organization_id=sample_case.organization_id,
            updates={"metadata": new_metadata},
        )

        assert updated.metadata == new_metadata


# ============================================================
# Error Handling Tests
# ============================================================


class TestErrorHandling:
    """Test error handling scenarios."""

    @pytest.mark.asyncio
    async def test_upload_to_nonexistent_case(self, evidence_service, sample_file_data):
        """Test uploading to a case that doesn't exist."""
        with pytest.raises(NotFoundError):
            await evidence_service.upload_evidence(
                case_id="nonexistent_case",
                organization_id="org_integration_test",
                user_id="user_integration_test",
                file_data=sample_file_data,
                original_filename="test.txt",
                mime_type="text/plain",
                evidence_type=EvidenceArtifactType.LOG_FILE,
            )

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="Requires unimplemented get_standalone_evidence method in DatabaseCaseRepository"
    )
    async def test_download_nonexistent_evidence(self, evidence_service):
        """Test downloading evidence that doesn't exist."""
        with pytest.raises(NotFoundError):
            await evidence_service.download_evidence(
                evidence_id="nonexistent_evidence",
                organization_id="org_integration_test",
            )


# ============================================================
# Health Check Tests
# ============================================================


class TestHealthCheck:
    """Test service health check."""

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, evidence_service):
        """Test that health check returns healthy status."""
        health = await evidence_service.health_check()

        assert health["status"] == "healthy"
        assert "file_storage" in health
