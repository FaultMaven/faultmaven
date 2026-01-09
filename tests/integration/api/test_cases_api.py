"""Integration tests for Case Management API (TASK-014, TASK-020).

Tests:
- POST /api/v1/cases (201 Created)
- GET /api/v1/cases/{case_id} (200 OK)
- GET /api/v1/cases/{case_id} (404 Not Found)
- GET /api/v1/cases (200 OK, list)
- GET /api/v1/cases?status=OPEN (filter)
- PATCH /api/v1/cases/{case_id} (200 OK)
- PATCH /api/v1/cases/{case_id} (403 Forbidden, wrong org)
- DELETE /api/v1/cases/{case_id} (204 No Content)
- POST /api/v1/cases/{case_id}/assign (200 OK)
- POST /api/v1/cases/{case_id}/close (200 OK)
- POST /api/v1/cases/{case_id}/reopen (200 OK)
- Missing JWT token returns 401

Note: As of TASK-020, JWT authentication is required. Legacy header authentication
(X-Organization-ID, X-User-ID) has been removed.
"""

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import Case, CaseSeverity, CaseStatus


@pytest.fixture
def mock_user():
    """Create a mock authenticated user for testing."""
    return AuthenticatedUser(
        user_id="user_789",
        organization_id="org_456",
        email="test@example.com",
        roles=["admin"],
        permissions=["cases:read", "cases:write", "cases:delete"],
    )


@pytest.fixture
def mock_case():
    """Create a mock Case for testing.

    Note: All optional fields must be explicitly set to None (not MagicMock)
    to ensure Pydantic validation passes in CaseResponse.from_domain().
    """
    mock = MagicMock()
    mock.case_id = "case_123abc"
    mock.organization_id = "org_456"
    mock.user_id = "user_789"
    mock.title = "Test Case"
    mock.description = "Test Description"
    mock.status = CaseStatus.CONSULTING
    mock.created_at = datetime.now(timezone.utc)
    mock.updated_at = datetime.now(timezone.utc)
    # Optional fields - must be explicit to avoid MagicMock being returned
    mock.problem_verification = None
    mock.closure_reason = None
    mock.metadata = None
    mock.assigned_to = None  # Optional[str]
    mock.closed_at = None  # Optional[datetime]
    mock.severity = CaseSeverity.MEDIUM  # Used by from_domain
    return mock


@pytest.fixture
def mock_case_service():
    """Create a mock case service."""
    service = AsyncMock()
    return service


@pytest.fixture
def app(mock_case_service, mock_user):
    """Create test application with mocked dependencies."""
    app = main_app

    # Override the case service dependency
    async def get_mock_case_service():
        return mock_case_service

    # Override the auth dependency to return mock user
    async def get_mock_current_user():
        return mock_user

    from faultmaven.api.dependencies import get_api_case_service
    from faultmaven.api.middleware.auth import get_current_user

    app.dependency_overrides[get_api_case_service] = get_mock_case_service
    app.dependency_overrides[get_current_user] = get_mock_current_user

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def headers():
    """Standard request headers with JWT auth (mocked via dependency override)."""
    # No special headers needed since auth is mocked via dependency override
    return {}


# ============================================================
# POST /api/v1/cases Tests
# ============================================================


class TestCreateCase:
    """Tests for POST /api/v1/cases endpoint."""

    def test_create_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case creation."""
        mock_case_service.create_case.return_value = mock_case

        response = client.post(
            "/api/v1/cases",
            json={
                "title": "Test Case",
                "description": "Test Description",
                "severity": "medium",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["case_id"] == "case_123abc"
        assert data["title"] == "Test Case"
        mock_case_service.create_case.assert_called_once()

    def test_create_case_with_metadata(self, client, mock_case_service, mock_case, headers):
        """Test case creation with metadata."""
        mock_case.metadata = {"source": "api"}
        mock_case_service.create_case.return_value = mock_case

        response = client.post(
            "/api/v1/cases",
            json={
                "title": "Test Case",
                "description": "Test",
                "severity": "high",
                "metadata": {"source": "api"},
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_create_case_missing_title(self, client, headers):
        """Test case creation without title."""
        response = client.post(
            "/api/v1/cases",
            json={
                "description": "Test",
                "severity": "low",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_case_empty_title(self, client, headers):
        """Test case creation with empty title."""
        response = client.post(
            "/api/v1/cases",
            json={
                "title": "",
                "description": "Test",
                "severity": "medium",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_create_case_missing_authentication(self):
        """Test case creation without JWT authentication returns 401."""
        # Create app without auth override to test unauthenticated request
        from faultmaven.main import app as main_app
        from fastapi.testclient import TestClient

        app = main_app
        unauthenticated_client = TestClient(app)

        response = unauthenticated_client.post(
            "/api/v1/cases",
            json={
                "title": "Test",
                "description": "Test",
                "severity": "medium",
            },
        )

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_create_case_all_severities(self, client, mock_case_service, mock_case, headers):
        """Test case creation with all severity levels."""
        mock_case_service.create_case.return_value = mock_case

        for severity in ["low", "medium", "high", "critical"]:
            response = client.post(
                "/api/v1/cases",
                json={
                    "title": "Test",
                    "description": "Test",
                    "severity": severity,
                },
                headers=headers,
            )
            assert response.status_code == status.HTTP_201_CREATED

    def test_create_case_invalid_severity(self, client, headers):
        """Test case creation with invalid severity."""
        response = client.post(
            "/api/v1/cases",
            json={
                "title": "Test",
                "description": "Test",
                "severity": "invalid",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ============================================================
# GET /api/v1/cases/{case_id} Tests
# ============================================================


class TestGetCase:
    """Tests for GET /api/v1/cases/{case_id} endpoint."""

    def test_get_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case retrieval."""
        mock_case_service.get_case.return_value = mock_case

        response = client.get(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["case_id"] == "case_123abc"
        mock_case_service.get_case.assert_called_once_with(
            "case_123abc", "org_456"
        )

    def test_get_case_not_found(self, client, mock_case_service, headers):
        """Test case not found."""
        mock_case_service.get_case.return_value = None

        response = client.get(
            "/api/v1/cases/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        data = response.json()
        assert data["error"] == "Not Found"
        assert "nonexistent" in data["detail"]

    def test_get_case_missing_authentication(self):
        """Test get case without JWT authentication returns 401."""
        # Create app without auth override to test unauthenticated request
        from faultmaven.main import app as main_app
        from fastapi.testclient import TestClient

        app = main_app
        unauthenticated_client = TestClient(app)

        response = unauthenticated_client.get("/api/v1/cases/case_123")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ============================================================
# GET /api/v1/cases Tests (List)
# ============================================================


class TestListCases:
    """Tests for GET /api/v1/cases endpoint."""

    def test_list_cases_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case list."""
        mock_case_service.list_cases.return_value = [mock_case]

        response = client.get(
            "/api/v1/cases",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "items" in data
        assert len(data["items"]) == 1
        assert data["items"][0]["case_id"] == "case_123abc"

    def test_list_cases_empty(self, client, mock_case_service, headers):
        """Test empty case list."""
        mock_case_service.list_cases.return_value = []

        response = client.get(
            "/api/v1/cases",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_cases_with_status_filter(self, client, mock_case_service, mock_case, headers):
        """Test case list with status filter."""
        mock_case_service.list_cases.return_value = [mock_case]

        response = client.get(
            "/api/v1/cases?status=consulting",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.list_cases.assert_called_once()
        call_kwargs = mock_case_service.list_cases.call_args.kwargs
        assert call_kwargs["status"] == CaseStatus.CONSULTING

    def test_list_cases_with_severity_filter(self, client, mock_case_service, mock_case, headers):
        """Test case list with severity filter."""
        mock_case_service.list_cases.return_value = [mock_case]

        response = client.get(
            "/api/v1/cases?severity=high",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.list_cases.assert_called_once()
        call_kwargs = mock_case_service.list_cases.call_args.kwargs
        assert call_kwargs["severity"] == CaseSeverity.HIGH

    def test_list_cases_pagination(self, client, mock_case_service, headers):
        """Test case list pagination."""
        mock_case_service.list_cases.return_value = []

        response = client.get(
            "/api/v1/cases?limit=10&offset=20",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 20

    def test_list_cases_limit_validation(self, client, headers):
        """Test case list limit validation."""
        # Limit too high
        response = client.get(
            "/api/v1/cases?limit=200",
            headers=headers,
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # Limit too low
        response = client.get(
            "/api/v1/cases?limit=0",
            headers=headers,
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ============================================================
# PATCH /api/v1/cases/{case_id} Tests
# ============================================================


class TestUpdateCase:
    """Tests for PATCH /api/v1/cases/{case_id} endpoint."""

    def test_update_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case update."""
        mock_case.title = "Updated Title"
        mock_case_service.update_case.return_value = mock_case

        response = client.patch(
            "/api/v1/cases/case_123abc",
            json={"title": "Updated Title"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Title"

    def test_update_case_multiple_fields(self, client, mock_case_service, mock_case, headers):
        """Test updating multiple fields."""
        mock_case.title = "Updated"
        mock_case.status = CaseStatus.INVESTIGATING
        mock_case_service.update_case.return_value = mock_case

        response = client.patch(
            "/api/v1/cases/case_123abc",
            json={
                "title": "Updated",
                "status": "investigating",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK

    def test_update_case_not_found(self, client, mock_case_service, headers):
        """Test updating non-existent case."""
        from faultmaven.exceptions import NotFoundError
        mock_case_service.update_case.side_effect = NotFoundError("Case", "nonexistent")

        response = client.patch(
            "/api/v1/cases/nonexistent",
            json={"title": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_case_forbidden(self, client, mock_case_service, headers):
        """Test updating case from different organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_case_service.update_case.side_effect = AuthorizationError(
            "Case not accessible by organization"
        )

        response = client.patch(
            "/api/v1/cases/case_123abc",
            json={"title": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_update_case_empty_request(self, client, mock_case_service, mock_case, headers):
        """Test update with empty request body."""
        mock_case_service.get_case.return_value = mock_case

        response = client.patch(
            "/api/v1/cases/case_123abc",
            json={},
            headers=headers,
        )

        # Should return current case without updates
        assert response.status_code == status.HTTP_200_OK


# ============================================================
# DELETE /api/v1/cases/{case_id} Tests
# ============================================================


class TestDeleteCase:
    """Tests for DELETE /api/v1/cases/{case_id} endpoint."""

    def test_delete_case_success(self, client, mock_case_service, headers):
        """Test successful case deletion."""
        mock_case_service.delete_case.return_value = True

        response = client.delete(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    def test_delete_case_not_found(self, client, mock_case_service, headers):
        """Test deleting non-existent case."""
        mock_case_service.delete_case.return_value = False

        response = client.delete(
            "/api/v1/cases/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_case_forbidden(self, client, mock_case_service, headers):
        """Test deleting case from different organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_case_service.delete_case.side_effect = AuthorizationError(
            "Not authorized"
        )

        response = client.delete(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


# ============================================================
# POST /api/v1/cases/{case_id}/assign Tests
# ============================================================


class TestAssignCase:
    """Tests for POST /api/v1/cases/{case_id}/assign endpoint."""

    def test_assign_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case assignment."""
        mock_case_service.assign_case.return_value = mock_case

        response = client.post(
            "/api/v1/cases/case_123abc/assign",
            json={"assigned_to": "user_assigned"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.assign_case.assert_called_once_with(
            case_id="case_123abc",
            organization_id="org_456",
            assigned_to="user_assigned",
        )

    def test_assign_case_not_found(self, client, mock_case_service, headers):
        """Test assigning non-existent case."""
        from faultmaven.exceptions import NotFoundError
        mock_case_service.assign_case.side_effect = NotFoundError("Case", "nonexistent")

        response = client.post(
            "/api/v1/cases/nonexistent/assign",
            json={"assigned_to": "user_assigned"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_assign_case_missing_assignee(self, client, headers):
        """Test assigning without assignee."""
        response = client.post(
            "/api/v1/cases/case_123abc/assign",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ============================================================
# POST /api/v1/cases/{case_id}/close Tests
# ============================================================


class TestCloseCase:
    """Tests for POST /api/v1/cases/{case_id}/close endpoint."""

    def test_close_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case closure."""
        mock_case.status = CaseStatus.RESOLVED
        mock_case_service.close_case.return_value = mock_case

        response = client.post(
            "/api/v1/cases/case_123abc/close",
            json={"resolution": "Issue was fixed"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.close_case.assert_called_once()

    def test_close_case_already_closed(self, client, mock_case_service, headers):
        """Test closing already closed case."""
        from faultmaven.exceptions import ConflictError
        mock_case_service.close_case.side_effect = ConflictError(
            "Case already closed",
            resource_type="Case",
            resource_id="case_123abc",
            conflict_reason="already_closed",
        )

        response = client.post(
            "/api/v1/cases/case_123abc/close",
            json={"resolution": "Fixed"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_close_case_missing_resolution(self, client, headers):
        """Test closing without resolution."""
        response = client.post(
            "/api/v1/cases/case_123abc/close",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ============================================================
# POST /api/v1/cases/{case_id}/reopen Tests
# ============================================================


class TestReopenCase:
    """Tests for POST /api/v1/cases/{case_id}/reopen endpoint."""

    def test_reopen_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case reopening."""
        mock_case.status = CaseStatus.CONSULTING
        mock_case_service.reopen_case.return_value = mock_case

        response = client.post(
            "/api/v1/cases/case_123abc/reopen",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "consulting"

    def test_reopen_case_not_closed(self, client, mock_case_service, headers):
        """Test reopening case that's not closed."""
        from faultmaven.exceptions import ConflictError
        mock_case_service.reopen_case.side_effect = ConflictError(
            "Case not closed",
            resource_type="Case",
            resource_id="case_123abc",
            conflict_reason="not_closed",
        )

        response = client.post(
            "/api/v1/cases/case_123abc/reopen",
            headers=headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT


# ============================================================
# GET /api/v1/cases/{case_id}/statistics Tests
# ============================================================


class TestCaseStatistics:
    """Tests for GET /api/v1/cases/{case_id}/statistics endpoint."""

    def test_get_case_statistics_success(self, client, mock_case_service, mock_case, headers):
        """Test successful statistics retrieval."""
        mock_case_service.get_case.return_value = mock_case
        mock_case_service.get_case_with_details.return_value = {
            "case": mock_case,
            "sessions": [],
            "evidence": [],
            "executions": [],
        }

        response = client.get(
            "/api/v1/cases/case_123abc/statistics",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "case_id" in data
        assert "session_count" in data
        assert "evidence_count" in data

    def test_get_case_statistics_not_found(self, client, mock_case_service, headers):
        """Test statistics for non-existent case."""
        mock_case_service.get_case.return_value = None

        response = client.get(
            "/api/v1/cases/nonexistent/statistics",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# Error Handling Tests
# ============================================================


class TestErrorHandling:
    """Tests for error handling in case endpoints."""

    def test_service_error_returns_500(self, client, mock_case_service, headers):
        """Test that service errors return 500."""
        from faultmaven.exceptions import ServiceError
        mock_case_service.create_case.side_effect = ServiceError("Database error")

        response = client.post(
            "/api/v1/cases",
            json={
                "title": "Test",
                "description": "Test",
                "severity": "medium",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    def test_validation_error_returns_400(self, client, mock_case_service, headers):
        """Test that validation errors return 400."""
        from faultmaven.exceptions import ValidationException
        mock_case_service.create_case.side_effect = ValidationException(
            "title: Title is required"
        )

        response = client.post(
            "/api/v1/cases",
            json={
                "title": "Test",
                "description": "Test",
                "severity": "medium",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
