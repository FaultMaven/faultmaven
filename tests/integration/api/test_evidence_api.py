"""Integration tests for Evidence Artifact API (TASK-014).

Tests:
- POST /api/v1/cases/{case_id}/evidence (201 Created, multipart upload)
- GET /api/v1/cases/{case_id}/evidence/{evidence_id} (200 OK)
- GET /api/v1/cases/{case_id}/evidence/{evidence_id}/download (200 OK, file stream)
- GET /api/v1/cases/{case_id}/evidence (200 OK, list)
- PATCH /api/v1/cases/{case_id}/evidence/{evidence_id} (200 OK)
- DELETE /api/v1/cases/{case_id}/evidence/{evidence_id} (204 No Content)
- POST /api/v1/cases/{case_id}/evidence/{evidence_id}/set-primary (200 OK)
- File upload with actual binary data
- Download returns correct content-type and filename
- Authorization errors (403 Forbidden)
"""

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from faultmaven.api.app import create_app
from faultmaven.models.evidence_artifact import EvidenceArtifactType


@pytest.fixture
def mock_evidence():
    """Create a mock EvidenceArtifact for testing."""
    mock = MagicMock()
    mock.evidence_id = "evd_123abc"
    mock.case_id = "case_456def"
    mock.user_id = "user_789"
    mock.organization_id = "org_456"
    mock.original_filename = "screenshot.png"
    mock.evidence_type = EvidenceArtifactType.SCREENSHOT
    mock.mime_type = "image/png"
    mock.file_size = 1024
    mock.description = "Login error screenshot"
    mock.is_primary = False
    mock.created_at = datetime.now(timezone.utc)
    mock.updated_at = datetime.now(timezone.utc)
    return mock


@pytest.fixture
def mock_evidence_service():
    """Create a mock evidence service."""
    service = AsyncMock()
    return service


@pytest.fixture
def app(mock_evidence_service):
    """Create test application with mocked dependencies."""
    app = create_app()

    async def get_mock_evidence_service():
        return mock_evidence_service

    from faultmaven.api.dependencies import get_evidence_artifact_service
    app.dependency_overrides[get_evidence_artifact_service] = get_mock_evidence_service

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def headers():
    """Standard request headers."""
    return {
        "X-Organization-ID": "org_456",
        "X-User-ID": "user_789",
    }


@pytest.fixture
def sample_file_content():
    """Sample file content for upload tests."""
    return b"fake image binary content for testing"


# ============================================================
# POST /api/v1/cases/{case_id}/evidence Tests
# ============================================================


class TestUploadEvidence:
    """Tests for POST /api/v1/cases/{case_id}/evidence endpoint."""

    def test_upload_evidence_success(
        self, client, mock_evidence_service, mock_evidence, headers, sample_file_content
    ):
        """Test successful evidence upload."""
        mock_evidence_service.upload_evidence.return_value = mock_evidence

        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            files={"file": ("screenshot.png", io.BytesIO(sample_file_content), "image/png")},
            data={"evidence_type": "screenshot"},
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["evidence_id"] == "evd_123abc"
        assert data["original_filename"] == "screenshot.png"
        mock_evidence_service.upload_evidence.assert_called_once()

    def test_upload_evidence_with_description(
        self, client, mock_evidence_service, mock_evidence, headers, sample_file_content
    ):
        """Test evidence upload with description."""
        mock_evidence_service.upload_evidence.return_value = mock_evidence

        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            files={"file": ("error.log", io.BytesIO(sample_file_content), "text/plain")},
            data={
                "evidence_type": "log_file",
                "description": "Error log from production",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_upload_evidence_as_primary(
        self, client, mock_evidence_service, mock_evidence, headers, sample_file_content
    ):
        """Test uploading evidence as primary."""
        mock_evidence.is_primary = True
        mock_evidence_service.upload_evidence.return_value = mock_evidence

        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            files={"file": ("main.png", io.BytesIO(sample_file_content), "image/png")},
            data={
                "evidence_type": "screenshot",
                "is_primary": "true",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["is_primary"] is True

    def test_upload_evidence_case_not_found(
        self, client, mock_evidence_service, headers, sample_file_content
    ):
        """Test upload to non-existent case."""
        from faultmaven.exceptions import NotFoundError
        mock_evidence_service.upload_evidence.side_effect = NotFoundError(
            "Case", "nonexistent"
        )

        response = client.post(
            "/api/v1/cases/nonexistent/evidence",
            headers=headers,
            files={"file": ("test.png", io.BytesIO(sample_file_content), "image/png")},
            data={"evidence_type": "screenshot"},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_upload_evidence_missing_file(self, client, headers):
        """Test upload without file."""
        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            data={"evidence_type": "screenshot"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_upload_evidence_missing_type(self, client, headers, sample_file_content):
        """Test upload without evidence type."""
        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            files={"file": ("test.png", io.BytesIO(sample_file_content), "image/png")},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_upload_evidence_invalid_type(self, client, headers, sample_file_content):
        """Test upload with invalid evidence type."""
        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            files={"file": ("test.png", io.BytesIO(sample_file_content), "image/png")},
            data={"evidence_type": "invalid_type"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_upload_evidence_all_types(
        self, client, mock_evidence_service, mock_evidence, headers, sample_file_content
    ):
        """Test uploading with all evidence types."""
        mock_evidence_service.upload_evidence.return_value = mock_evidence

        evidence_types = [
            "screenshot",
            "log_file",
            "network_trace",
            "code_snippet",
            "configuration",
            "video_recording",
            "har_file",
            "crash_dump",
            "heap_dump",
            "thread_dump",
            "metrics_export",
            "other",
        ]

        for etype in evidence_types:
            response = client.post(
                "/api/v1/cases/case_456def/evidence",
                headers=headers,
                files={"file": ("test.bin", io.BytesIO(sample_file_content), "application/octet-stream")},
                data={"evidence_type": etype},
            )
            assert response.status_code == status.HTTP_201_CREATED

    def test_upload_evidence_missing_headers(self, client, sample_file_content):
        """Test upload without required headers."""
        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            files={"file": ("test.png", io.BytesIO(sample_file_content), "image/png")},
            data={"evidence_type": "screenshot"},
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ============================================================
# GET /api/v1/cases/{case_id}/evidence/{evidence_id} Tests
# ============================================================


class TestGetEvidence:
    """Tests for GET /api/v1/cases/{case_id}/evidence/{evidence_id} endpoint."""

    def test_get_evidence_success(self, client, mock_evidence_service, mock_evidence, headers):
        """Test successful evidence retrieval."""
        mock_evidence_service.get_evidence.return_value = mock_evidence

        response = client.get(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["evidence_id"] == "evd_123abc"
        assert data["original_filename"] == "screenshot.png"

    def test_get_evidence_not_found(self, client, mock_evidence_service, headers):
        """Test evidence not found."""
        mock_evidence_service.get_evidence.return_value = None

        response = client.get(
            "/api/v1/cases/case_456def/evidence/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_evidence_wrong_case(self, client, mock_evidence_service, mock_evidence, headers):
        """Test evidence retrieval with wrong case ID."""
        mock_evidence.case_id = "different_case"
        mock_evidence_service.get_evidence.return_value = mock_evidence

        response = client.get(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# GET /api/v1/cases/{case_id}/evidence/{evidence_id}/download Tests
# ============================================================


class TestDownloadEvidence:
    """Tests for GET /api/v1/cases/{case_id}/evidence/{evidence_id}/download endpoint."""

    def test_download_evidence_success(
        self, client, mock_evidence_service, mock_evidence, headers, sample_file_content
    ):
        """Test successful evidence download."""
        mock_evidence_service.get_evidence.return_value = mock_evidence
        mock_evidence_service.download_evidence.return_value = (
            sample_file_content,
            "screenshot.png",
            "image/png",
        )

        response = client.get(
            "/api/v1/cases/case_456def/evidence/evd_123abc/download",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"] == "image/png"
        assert 'attachment; filename="screenshot.png"' in response.headers["content-disposition"]
        assert response.content == sample_file_content

    def test_download_evidence_not_found(self, client, mock_evidence_service, headers):
        """Test download of non-existent evidence."""
        mock_evidence_service.get_evidence.return_value = None

        response = client.get(
            "/api/v1/cases/case_456def/evidence/nonexistent/download",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_download_evidence_various_mimetypes(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test download with various MIME types."""
        mimetypes = [
            ("text/plain", "error.log", b"log content"),
            ("application/json", "config.json", b'{"key": "value"}'),
            ("video/mp4", "recording.mp4", b"video content"),
        ]

        for mimetype, filename, content in mimetypes:
            mock_evidence.original_filename = filename
            mock_evidence.mime_type = mimetype
            mock_evidence_service.get_evidence.return_value = mock_evidence
            mock_evidence_service.download_evidence.return_value = (
                content, filename, mimetype
            )

            response = client.get(
                "/api/v1/cases/case_456def/evidence/evd_123abc/download",
                headers=headers,
            )

            assert response.status_code == status.HTTP_200_OK
            assert response.headers["content-type"] == mimetype


# ============================================================
# GET /api/v1/cases/{case_id}/evidence Tests (List)
# ============================================================


class TestListEvidence:
    """Tests for GET /api/v1/cases/{case_id}/evidence endpoint."""

    def test_list_evidence_success(self, client, mock_evidence_service, mock_evidence, headers):
        """Test successful evidence list."""
        mock_evidence_service.list_evidence_by_case.return_value = [mock_evidence]

        response = client.get(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["evidence_id"] == "evd_123abc"

    def test_list_evidence_empty(self, client, mock_evidence_service, headers):
        """Test empty evidence list."""
        mock_evidence_service.list_evidence_by_case.return_value = []

        response = client.get(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_list_evidence_with_type_filter(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test evidence list with type filter."""
        mock_evidence_service.list_evidence_by_case.return_value = [mock_evidence]

        response = client.get(
            "/api/v1/cases/case_456def/evidence?evidence_type=screenshot",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        call_kwargs = mock_evidence_service.list_evidence_by_case.call_args.kwargs
        assert call_kwargs["evidence_type"] == EvidenceArtifactType.SCREENSHOT

    def test_list_evidence_pagination(self, client, mock_evidence_service, headers):
        """Test evidence list pagination."""
        mock_evidence_service.list_evidence_by_case.return_value = []

        response = client.get(
            "/api/v1/cases/case_456def/evidence?limit=25&offset=10",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        call_kwargs = mock_evidence_service.list_evidence_by_case.call_args.kwargs
        assert call_kwargs["limit"] == 25
        assert call_kwargs["offset"] == 10

    def test_list_evidence_case_not_found(self, client, mock_evidence_service, headers):
        """Test listing evidence for non-existent case."""
        from faultmaven.exceptions import NotFoundError
        mock_evidence_service.list_evidence_by_case.side_effect = NotFoundError(
            "Case", "nonexistent"
        )

        response = client.get(
            "/api/v1/cases/nonexistent/evidence",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# PATCH /api/v1/cases/{case_id}/evidence/{evidence_id} Tests
# ============================================================


class TestUpdateEvidence:
    """Tests for PATCH /api/v1/cases/{case_id}/evidence/{evidence_id} endpoint."""

    def test_update_evidence_success(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test successful evidence update."""
        mock_evidence.description = "Updated description"
        mock_evidence_service.get_evidence.return_value = mock_evidence
        mock_evidence_service.update_evidence.return_value = mock_evidence

        response = client.patch(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            json={"description": "Updated description"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["description"] == "Updated description"

    def test_update_evidence_set_primary(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test setting evidence as primary via update."""
        mock_evidence.is_primary = True
        mock_evidence_service.get_evidence.return_value = mock_evidence
        mock_evidence_service.update_evidence.return_value = mock_evidence

        response = client.patch(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            json={"is_primary": True},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK

    def test_update_evidence_not_found(self, client, mock_evidence_service, headers):
        """Test updating non-existent evidence."""
        mock_evidence_service.get_evidence.return_value = None

        response = client.patch(
            "/api/v1/cases/case_456def/evidence/nonexistent",
            json={"description": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_evidence_empty_request(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test update with empty request."""
        mock_evidence_service.get_evidence.return_value = mock_evidence

        response = client.patch(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK


# ============================================================
# DELETE /api/v1/cases/{case_id}/evidence/{evidence_id} Tests
# ============================================================


class TestDeleteEvidence:
    """Tests for DELETE /api/v1/cases/{case_id}/evidence/{evidence_id} endpoint."""

    def test_delete_evidence_success(self, client, mock_evidence_service, mock_evidence, headers):
        """Test successful evidence deletion."""
        mock_evidence_service.get_evidence.return_value = mock_evidence
        mock_evidence_service.delete_evidence.return_value = True

        response = client.delete(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_evidence_not_found(self, client, mock_evidence_service, headers):
        """Test deleting non-existent evidence."""
        mock_evidence_service.get_evidence.return_value = None

        response = client.delete(
            "/api/v1/cases/case_456def/evidence/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_evidence_wrong_case(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test deleting evidence from wrong case."""
        mock_evidence.case_id = "different_case"
        mock_evidence_service.get_evidence.return_value = mock_evidence

        response = client.delete(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# POST /api/v1/cases/{case_id}/evidence/{evidence_id}/set-primary Tests
# ============================================================


class TestSetPrimaryEvidence:
    """Tests for POST /api/v1/cases/{case_id}/evidence/{evidence_id}/set-primary endpoint."""

    def test_set_primary_success(self, client, mock_evidence_service, mock_evidence, headers):
        """Test setting evidence as primary."""
        mock_evidence.is_primary = True
        mock_evidence_service.get_evidence.return_value = mock_evidence
        mock_evidence_service.set_primary_evidence.return_value = mock_evidence

        response = client.post(
            "/api/v1/cases/case_456def/evidence/evd_123abc/set-primary",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["is_primary"] is True

    def test_set_primary_not_found(self, client, mock_evidence_service, headers):
        """Test setting primary on non-existent evidence."""
        mock_evidence_service.get_evidence.return_value = None

        response = client.post(
            "/api/v1/cases/case_456def/evidence/nonexistent/set-primary",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_set_primary_wrong_case(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test setting primary for evidence from wrong case."""
        mock_evidence.case_id = "different_case"
        mock_evidence_service.get_evidence.return_value = mock_evidence

        response = client.post(
            "/api/v1/cases/case_456def/evidence/evd_123abc/set-primary",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# Authorization Tests
# ============================================================


class TestEvidenceAuthorization:
    """Tests for evidence authorization."""

    def test_upload_evidence_forbidden(
        self, client, mock_evidence_service, headers, sample_file_content
    ):
        """Test upload with wrong organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_evidence_service.upload_evidence.side_effect = AuthorizationError(
            "Case not accessible by organization"
        )

        response = client.post(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
            files={"file": ("test.png", io.BytesIO(sample_file_content), "image/png")},
            data={"evidence_type": "screenshot"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_list_evidence_forbidden(self, client, mock_evidence_service, headers):
        """Test listing evidence with wrong organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_evidence_service.list_evidence_by_case.side_effect = AuthorizationError(
            "Not authorized"
        )

        response = client.get(
            "/api/v1/cases/case_456def/evidence",
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_delete_evidence_forbidden(
        self, client, mock_evidence_service, mock_evidence, headers
    ):
        """Test deleting evidence with wrong organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_evidence_service.get_evidence.return_value = mock_evidence
        mock_evidence_service.delete_evidence.side_effect = AuthorizationError(
            "Not authorized"
        )

        response = client.delete(
            "/api/v1/cases/case_456def/evidence/evd_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
