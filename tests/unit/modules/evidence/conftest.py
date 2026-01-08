"""Fixtures for EvidenceArtifact Service module tests (PR #46c)."""
import pytest
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceListFilter,
    EvidenceLinkRequest,
    EvidenceUploadRequest,
    StorageBackend,
)


# =============================================================================
# Sample Data Factories
# =============================================================================

def create_sample_evidence(
    evidence_id: Optional[str] = None,
    case_id: Optional[str] = None,
    user_id: Optional[str] = None,
    organization_id: str = "org_test123",
    original_filename: str = "test_file.log",
    stored_filename: Optional[str] = None,
    file_path: str = "evidence/standalone-abc123/2025-01-02/uuid_test_file.log",
    evidence_type: EvidenceArtifactType = EvidenceArtifactType.LOG_FILE,
    mime_type: str = "text/plain",
    file_size: int = 1024,
    storage_backend: StorageBackend = StorageBackend.LOCAL_FILESYSTEM,
    description: Optional[str] = "Test evidence file",
    metadata: Optional[dict] = None,
    # Backward compatibility: Accept old parameter names
    filename: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    tags: Optional[list] = None,
) -> EvidenceArtifact:
    """Create a sample EvidenceArtifact object for testing.

    Supports both new field names (evidence_id, user_id, original_filename)
    and old field names (id, uploaded_by, filename) for backward compatibility.
    """
    # Handle backward compatibility parameters
    if filename is not None:
        original_filename = filename
    if uploaded_by is not None:
        user_id = uploaded_by
    if tags is not None:
        if metadata is None:
            metadata = {}
        metadata["tags"] = tags

    eid = evidence_id or str(uuid4())
    return EvidenceArtifact(
        evidence_id=eid,
        case_id=case_id or str(uuid4()),
        user_id=user_id or str(uuid4()),
        organization_id=organization_id,
        original_filename=original_filename,
        stored_filename=stored_filename or f"{eid}_{original_filename}",
        file_path=file_path,
        evidence_type=evidence_type,
        mime_type=mime_type,
        file_size=file_size,
        storage_backend=storage_backend,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        description=description,
        metadata=metadata or {},
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
        # Accept both old and new parameter names
        original_filename: Optional[str] = None,
        mime_type: Optional[str] = None,
        file_size: Optional[int] = None,
        file_path: Optional[str] = None,
        user_id: Optional[str] = None,
        case_id: Optional[str] = None,
        organization_id: str = "org_test123",
        description: Optional[str] = None,
        # Old parameter names for backward compatibility
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        storage_path: Optional[str] = None,
        uploaded_by: Optional[str] = None,
        tags: Optional[List[str]] = None,
        **kwargs
    ) -> EvidenceArtifact:
        # Map old params to new params
        evidence = create_sample_evidence(
            original_filename=original_filename or filename or "test.log",
            mime_type=mime_type or content_type or "text/plain",
            file_size=file_size or size_bytes or 1024,
            file_path=file_path or storage_path or "evidence/test.log",
            user_id=user_id or (str(uploaded_by) if uploaded_by else None) or str(uuid4()),
            case_id=case_id or "standalone",
            organization_id=organization_id,
            description=description,
            metadata={"tags": tags or []} if tags else {},
        )
        self._storage[evidence.evidence_id] = evidence
        return evidence

    async def _get(self, evidence_id) -> Optional[EvidenceArtifact]:
        # Accept both UUID and str
        key = str(evidence_id) if not isinstance(evidence_id, str) else evidence_id
        return self._storage.get(key)

    async def _list(self, filters: EvidenceListFilter) -> Tuple[List[EvidenceArtifact], int]:
        results = list(self._storage.values())

        # Apply filters
        if filters.uploaded_by:
            results = [e for e in results if e.user_id == str(filters.uploaded_by)]
        if filters.case_id:
            results = [e for e in results if e.case_id == str(filters.case_id)]
        if filters.tags:
            # Tags are in metadata
            results = [
                e for e in results
                if e.metadata and any(tag in e.metadata.get("tags", []) for tag in filters.tags)
            ]
        if filters.filename_contains:
            results = [e for e in results if filters.filename_contains.lower() in e.original_filename.lower()]

        total = len(results)
        results = results[filters.offset : filters.offset + filters.limit]
        return results, total

    async def _delete(self, evidence_id) -> bool:
        # Accept both UUID and str
        key = str(evidence_id) if not isinstance(evidence_id, str) else evidence_id
        if key in self._storage:
            del self._storage[key]
            return True
        return False

    async def _link_to_case(self, evidence_id, case_id) -> Optional[EvidenceArtifact]:
        # Accept both UUID and str
        key = str(evidence_id) if not isinstance(evidence_id, str) else evidence_id
        evidence = self._storage.get(key)
        if evidence:
            # Update case_id if needed
            evidence.case_id = str(case_id) if not isinstance(case_id, str) else case_id
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
    user_id = str(uuid4())
    case_id = str(uuid4())
    return [
        create_sample_evidence(
            original_filename=f"file_{i}.log",
            user_id=user_id,
            case_id=case_id,
            metadata={"tags": [f"tag{i}"]},  # Add unique tag for each
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
