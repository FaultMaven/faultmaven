"""Fixtures for EvidenceArtifact Service module tests (PR #46c)."""
import pytest
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceListFilter,
    EvidenceLinkRequest,
    EvidenceUploadRequest,
)


# =============================================================================
# Sample Data Factories
# =============================================================================

def create_sample_evidence(
    evidence_id: Optional[UUID] = None,
    filename: str = "test_file.log",
    content_type: str = "text/plain",
    size_bytes: int = 1024,
    storage_path: str = "evidence/standalone-abc123/2025-01-02/uuid_test_file.log",
    uploaded_by: Optional[UUID] = None,
    description: Optional[str] = "Test evidence file",
    tags: Optional[List[str]] = None,
    linked_cases: Optional[List[UUID]] = None,
) -> EvidenceArtifact:
    """Create a sample EvidenceArtifact object for testing."""
    return EvidenceArtifact(
        id=evidence_id or uuid4(),
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        storage_path=storage_path,
        uploaded_by=uploaded_by or uuid4(),
        uploaded_at=datetime.now(timezone.utc),
        description=description,
        tags=tags or ["test", "sample"],
        linked_cases=linked_cases or [],
        metadata={},
    )


def create_sample_upload_request(
    filename: str = "upload.log",
    content_type: str = "text/plain",
    description: Optional[str] = "Uploaded file",
    tags: Optional[List[str]] = None,
    case_id: Optional[UUID] = None,
) -> EvidenceUploadRequest:
    """Create a sample EvidenceUploadRequest for testing."""
    return EvidenceUploadRequest(
        filename=filename,
        content_type=content_type,
        description=description,
        tags=tags or [],
        case_id=case_id,
    )


# =============================================================================
# Mock Classes
# =============================================================================

class MockEvidenceStorageAdapter:
    """Mock storage adapter for testing EvidenceService."""

    def __init__(self):
        self.store_file = AsyncMock(return_value="evidence/standalone-abc123/2025-01-02/uuid_test.log")
        self.delete_file = AsyncMock(return_value=True)
        self.get_download_url = AsyncMock(return_value="http://localhost:8000/api/v1/evidence/file/path")
        self.get_file_content = AsyncMock(return_value=b"file content")


class MockEvidenceRepository:
    """Mock repository for testing EvidenceService."""

    def __init__(self):
        self._storage: dict[str, EvidenceArtifact] = {}
        self.create = AsyncMock(side_effect=self._create)
        self.get = AsyncMock(side_effect=self._get)
        self.list = AsyncMock(side_effect=self._list)
        self.delete = AsyncMock(side_effect=self._delete)
        self.link_to_case = AsyncMock(side_effect=self._link_to_case)

    async def _create(
        self,
        filename: str,
        content_type: str,
        size_bytes: int,
        storage_path: str,
        uploaded_by: UUID,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> EvidenceArtifact:
        evidence = create_sample_evidence(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_path=storage_path,
            uploaded_by=uploaded_by,
            description=description,
            tags=tags or [],
        )
        self._storage[str(evidence.id)] = evidence
        return evidence

    async def _get(self, evidence_id: UUID) -> Optional[EvidenceArtifact]:
        return self._storage.get(str(evidence_id))

    async def _list(self, filters: EvidenceListFilter) -> Tuple[List[EvidenceArtifact], int]:
        results = list(self._storage.values())

        # Apply filters
        if filters.uploaded_by:
            results = [e for e in results if e.uploaded_by == filters.uploaded_by]
        if filters.case_id:
            results = [e for e in results if filters.case_id in e.linked_cases]
        if filters.tags:
            results = [e for e in results if any(t in e.tags for t in filters.tags)]
        if filters.filename_contains:
            results = [e for e in results if filters.filename_contains.lower() in e.filename.lower()]

        total = len(results)
        results = results[filters.offset : filters.offset + filters.limit]
        return results, total

    async def _delete(self, evidence_id: UUID) -> bool:
        key = str(evidence_id)
        if key in self._storage:
            del self._storage[key]
            return True
        return False

    async def _link_to_case(self, evidence_id: UUID, case_id: UUID) -> Optional[EvidenceArtifact]:
        evidence = self._storage.get(str(evidence_id))
        if evidence:
            if case_id not in evidence.linked_cases:
                evidence.linked_cases.append(case_id)
            return evidence
        return None


class MockUploadFile:
    """Mock FastAPI UploadFile for testing."""

    def __init__(
        self,
        filename: str = "test.log",
        content_type: str = "text/plain",
        content: bytes = b"test file content",
    ):
        self.filename = filename
        self.content_type = content_type
        self._content = content
        self.size = len(content)

    async def read(self) -> bytes:
        return self._content

    async def seek(self, offset: int) -> None:
        pass


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sample_evidence() -> EvidenceArtifact:
    """Create a sample EvidenceArtifact object."""
    return create_sample_evidence()


@pytest.fixture
def sample_evidence_list() -> List[EvidenceArtifact]:
    """Create multiple sample EvidenceArtifact objects."""
    user_id = uuid4()
    return [
        create_sample_evidence(
            filename=f"file_{i}.log",
            uploaded_by=user_id,
            tags=[f"tag{i}", "common"],
        )
        for i in range(5)
    ]


@pytest.fixture
def mock_storage() -> MockEvidenceStorageAdapter:
    """Create a mock storage adapter."""
    return MockEvidenceStorageAdapter()


@pytest.fixture
def mock_repository() -> MockEvidenceRepository:
    """Create a mock repository."""
    return MockEvidenceRepository()


@pytest.fixture
def mock_upload_file() -> MockUploadFile:
    """Create a mock upload file."""
    return MockUploadFile()


@pytest.fixture
def sample_user_id() -> UUID:
    """Create a sample user ID."""
    return uuid4()


@pytest.fixture
def sample_case_id() -> UUID:
    """Create a sample case ID."""
    return uuid4()


@pytest.fixture
def sample_filters() -> EvidenceListFilter:
    """Create sample list filters."""
    return EvidenceListFilter(
        limit=50,
        offset=0,
    )
