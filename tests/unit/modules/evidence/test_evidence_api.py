"""API integration tests for Evidence endpoints (PR #46c).

Tests the FastAPI endpoints with mocked services.
"""
import io
import pytest
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient

from faultmaven.modules.evidence.domain.models import EvidenceArtifact, EvidenceListFilter

from .conftest import create_sample_evidence


# =============================================================================
# Mock Service for API Tests
# =============================================================================

class MockEvidenceServiceForAPI:
    """Mock EvidenceService for API testing."""

    def __init__(self):
        self._storage: dict[str, EvidenceArtifact] = {}
        self.upload_evidence = AsyncMock(side_effect=self._upload)
        self.get_evidence = AsyncMock(side_effect=self._get)
        self.list_evidence = AsyncMock(side_effect=self._list)
        self.delete_evidence = AsyncMock(side_effect=self._delete)
        self.link_to_case = AsyncMock(side_effect=self._link)
        self.get_file_url = AsyncMock(side_effect=self._get_url)

    async def _upload(self, file, uploaded_by, description=None, tags=None, case_id=None):
        evidence = create_sample_evidence(
            filename=file.filename,
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
        return results[filters.offset:filters.offset + filters.limit], len(results)

    async def _delete(self, evidence_id: UUID) -> bool:
        key = str(evidence_id)
        if key in self._storage:
            del self._storage[key]
            return True
        return False

    async def _link(self, evidence_id: UUID, case_id: UUID) -> EvidenceArtifact:
        evidence = self._storage.get(str(evidence_id))
        if not evidence:
            raise ValueError(f"Evidence {evidence_id} not found")
        if case_id not in evidence.linked_cases:
            evidence.linked_cases.append(case_id)
        return evidence

    async def _get_url(self, evidence_id: UUID) -> Optional[str]:
        evidence = self._storage.get(str(evidence_id))
        if evidence:
            return f"http://localhost:8000/api/v1/evidence/file/{evidence.storage_path}"
        return None


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_evidence_service():
    """Create mock evidence service."""
    return MockEvidenceServiceForAPI()


@pytest.fixture
def mock_user():
    """Create a mock authenticated user."""
    class DevUser:
        def __init__(self):
            self.id = uuid4()
            self.user_id = self.id  # Some code uses user_id
            self.email = "test@example.com"
            self.organization_id = uuid4()
    return DevUser()


@pytest.fixture
def app_with_mocks(mock_evidence_service, mock_user):
    """Create FastAPI app with mocked dependencies."""
    from fastapi import FastAPI, Depends
    from faultmaven.modules.evidence.api.routes import router

    app = FastAPI()

    # Override dependencies
    def get_mock_service():
        return mock_evidence_service

    def get_mock_user():
        return mock_user

    # Include router with overridden dependencies
    app.include_router(router, prefix="/api/v1/evidence")

    # Store mocks for dependency override
    app.state.mock_service = mock_evidence_service
    app.state.mock_user = mock_user

    return app


@pytest.fixture
def client(app_with_mocks, mock_evidence_service, mock_user):
    """Create test client with mocked dependencies."""
    from faultmaven.modules.evidence.api.routes import get_evidence_service
    from faultmaven.api.dependencies import get_current_user

    app_with_mocks.dependency_overrides[get_evidence_service] = lambda: mock_evidence_service
    app_with_mocks.dependency_overrides[get_current_user] = lambda: mock_user

    return TestClient(app_with_mocks)


# =============================================================================
# Test Classes
# =============================================================================

class TestUploadEvidenceEndpoint:
    """Tests for POST /api/v1/evidence endpoint."""

    def test_upload_evidence_success(self, client, mock_evidence_service):
        """Test successful evidence upload."""
        file_content = b"test file content"
        files = {"file": ("test.log", io.BytesIO(file_content), "text/plain")}
        data = {"description": "Test upload"}

        response = client.post("/api/v1/evidence/", files=files, data=data)

        assert response.status_code == 201
        result = response.json()
        assert "id" in result
        assert result["filename"] == "test.log"

    def test_upload_evidence_with_tags(self, client, mock_evidence_service):
        """Test upload with comma-separated tags."""
        files = {"file": ("tagged.log", io.BytesIO(b"content"), "text/plain")}
        data = {"tags": "error,production,critical"}

        response = client.post("/api/v1/evidence/", files=files, data=data)

        assert response.status_code == 201

    def test_upload_evidence_with_case_id(self, client, mock_evidence_service):
        """Test upload with case_id for auto-linking."""
        case_id = str(uuid4())
        files = {"file": ("linked.log", io.BytesIO(b"content"), "text/plain")}
        data = {"case_id": case_id}

        response = client.post("/api/v1/evidence/", files=files, data=data)

        assert response.status_code == 201

    def test_upload_evidence_minimal(self, client, mock_evidence_service):
        """Test upload with only required file."""
        files = {"file": ("minimal.log", io.BytesIO(b"content"), "text/plain")}

        response = client.post("/api/v1/evidence/", files=files)

        assert response.status_code == 201


class TestGetEvidenceEndpoint:
    """Tests for GET /api/v1/evidence/{evidence_id} endpoint."""

    def test_get_evidence_success(self, client, mock_evidence_service):
        """Test retrieving existing evidence."""
        # Pre-populate service
        evidence = create_sample_evidence()
        mock_evidence_service._storage[str(evidence.id)] = evidence

        response = client.get(f"/api/v1/evidence/{evidence.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["id"] == str(evidence.id)

    def test_get_evidence_not_found(self, client, mock_evidence_service):
        """Test retrieving non-existent evidence returns 404."""
        non_existent_id = uuid4()

        response = client.get(f"/api/v1/evidence/{non_existent_id}")

        assert response.status_code == 404

    def test_get_evidence_invalid_uuid(self, client):
        """Test invalid UUID returns 422."""
        response = client.get("/api/v1/evidence/not-a-uuid")

        assert response.status_code == 422


class TestDeleteEvidenceEndpoint:
    """Tests for DELETE /api/v1/evidence/{evidence_id} endpoint."""

    def test_delete_evidence_success(self, client, mock_evidence_service):
        """Test deleting existing evidence."""
        evidence = create_sample_evidence()
        mock_evidence_service._storage[str(evidence.id)] = evidence

        response = client.delete(f"/api/v1/evidence/{evidence.id}")

        assert response.status_code == 204

    def test_delete_evidence_not_found(self, client, mock_evidence_service):
        """Test deleting non-existent evidence returns 404."""
        non_existent_id = uuid4()

        response = client.delete(f"/api/v1/evidence/{non_existent_id}")

        assert response.status_code == 404


class TestListEvidenceEndpoint:
    """Tests for GET /api/v1/evidence endpoint."""

    def test_list_evidence_empty(self, client, mock_evidence_service):
        """Test listing when no evidence exists."""
        response = client.get("/api/v1/evidence/")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_evidence_with_results(self, client, mock_evidence_service):
        """Test listing returns evidence."""
        # Pre-populate
        for i in range(3):
            evidence = create_sample_evidence(filename=f"file_{i}.log")
            mock_evidence_service._storage[str(evidence.id)] = evidence

        response = client.get("/api/v1/evidence/")

        assert response.status_code == 200
        result = response.json()
        assert len(result) == 3

    def test_list_evidence_with_pagination(self, client, mock_evidence_service):
        """Test listing with limit and offset."""
        for i in range(5):
            evidence = create_sample_evidence(filename=f"file_{i}.log")
            mock_evidence_service._storage[str(evidence.id)] = evidence

        response = client.get("/api/v1/evidence/?limit=2&offset=0")

        assert response.status_code == 200
        result = response.json()
        assert len(result) == 2

    def test_list_evidence_filter_by_tags(self, client, mock_evidence_service):
        """Test listing with tag filter."""
        response = client.get("/api/v1/evidence/?tags=error,warning")

        assert response.status_code == 200

    def test_list_evidence_filter_by_filename(self, client, mock_evidence_service):
        """Test listing with filename filter."""
        response = client.get("/api/v1/evidence/?filename_contains=error")

        assert response.status_code == 200


class TestGetEvidenceForCaseEndpoint:
    """Tests for GET /api/v1/evidence/case/{case_id} endpoint."""

    def test_get_evidence_for_case_success(self, client, mock_evidence_service):
        """Test getting evidence linked to a case."""
        case_id = uuid4()

        response = client.get(f"/api/v1/evidence/case/{case_id}")

        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_get_evidence_for_case_empty(self, client, mock_evidence_service):
        """Test getting evidence for case with no linked evidence."""
        case_id = uuid4()

        response = client.get(f"/api/v1/evidence/case/{case_id}")

        assert response.status_code == 200
        assert response.json() == []


class TestLinkEvidenceToCaseEndpoint:
    """Tests for POST /api/v1/evidence/{evidence_id}/link endpoint."""

    def test_link_to_case_success(self, client, mock_evidence_service):
        """Test linking evidence to a case."""
        evidence = create_sample_evidence()
        mock_evidence_service._storage[str(evidence.id)] = evidence
        case_id = uuid4()

        response = client.post(
            f"/api/v1/evidence/{evidence.id}/link",
            json={"case_id": str(case_id)},
        )

        assert response.status_code == 200
        result = response.json()
        assert str(case_id) in [str(c) for c in result["linked_cases"]]

    def test_link_to_case_not_found(self, client, mock_evidence_service):
        """Test linking non-existent evidence returns 404."""
        non_existent_id = uuid4()
        case_id = uuid4()

        response = client.post(
            f"/api/v1/evidence/{non_existent_id}/link",
            json={"case_id": str(case_id)},
        )

        assert response.status_code == 404

    def test_link_to_case_invalid_body(self, client, mock_evidence_service):
        """Test linking with invalid request body returns 422."""
        evidence = create_sample_evidence()
        mock_evidence_service._storage[str(evidence.id)] = evidence

        response = client.post(
            f"/api/v1/evidence/{evidence.id}/link",
            json={"invalid_field": "value"},
        )

        assert response.status_code == 422


class TestDownloadEvidenceEndpoint:
    """Tests for GET /api/v1/evidence/{evidence_id}/download endpoint."""

    def test_download_evidence_redirects(self, client, mock_evidence_service):
        """Test download redirects to file URL."""
        evidence = create_sample_evidence()
        mock_evidence_service._storage[str(evidence.id)] = evidence

        response = client.get(
            f"/api/v1/evidence/{evidence.id}/download",
            follow_redirects=False,
        )

        # Should redirect
        assert response.status_code in [302, 307]

    def test_download_evidence_not_found(self, client, mock_evidence_service):
        """Test downloading non-existent evidence returns 404."""
        non_existent_id = uuid4()

        response = client.get(f"/api/v1/evidence/{non_existent_id}/download")

        assert response.status_code == 404


class TestRouteOrdering:
    """Tests for route ordering (case/{case_id} before {evidence_id})."""

    def test_case_route_not_confused_with_evidence_id(self, client, mock_evidence_service):
        """Test that /case/{case_id} is not confused with /{evidence_id}."""
        case_id = uuid4()

        # This should hit /case/{case_id}, not /{evidence_id}
        response = client.get(f"/api/v1/evidence/case/{case_id}")

        # Should return 200 with list, not 404 for "case" as evidence_id
        assert response.status_code == 200
        assert isinstance(response.json(), list)
