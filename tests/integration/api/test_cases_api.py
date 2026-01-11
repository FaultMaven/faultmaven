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
from httpx import AsyncClient, ASGITransport

from faultmaven.main import app as main_app
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.models.api_models import CaseSummary
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
def mock_case_summary():
    """Create a mock CaseSummary for list endpoints."""
    now = datetime.now(timezone.utc)
    return CaseSummary(
        case_id="case_123abc",
        title="Test Case",
        status=CaseStatus.CONSULTING,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        user_id="user_789",
        organization_id="org_456",
        current_turn=1,
        milestones_completed=2,
        total_milestones=8,
        is_stuck=False,
        is_terminal=False
    )


@pytest.fixture
def mock_case_service():
    """Create a mock case service."""
    service = AsyncMock()
    return service


@pytest.fixture
def mock_session_service():
    """Create a mock session service."""
    service = AsyncMock()
    return service


@pytest.fixture
def app(mock_case_service, mock_session_service, mock_user):
    """Create test application with mocked dependencies."""
    app = main_app

    # Override the case service dependency
    async def get_mock_case_service():
        return mock_case_service

    # Override the session service dependency
    async def get_mock_session_service():
        return mock_session_service

    # Override the auth dependency to return mock user
    async def get_mock_current_user():
        return mock_user

    # Import the actual dependencies used by case routes
    # Note: Case routes use wrapper functions, so we override those
    from faultmaven.modules.case.api.routes import _di_get_case_service_dependency, _di_get_session_service_dependency
    from faultmaven.api.v1.auth_dependencies import require_authentication

    app.dependency_overrides[_di_get_case_service_dependency] = get_mock_case_service
    app.dependency_overrides[_di_get_session_service_dependency] = get_mock_session_service
    app.dependency_overrides[require_authentication] = get_mock_current_user

    yield app

    # Cleanup: clear overrides after test
    app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """Create async test client with proper lifespan handling."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        yield client


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

    async def test_create_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case creation."""
        mock_case_service.create_case.return_value = mock_case

        response = await client.post(
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

    async def test_create_case_with_metadata(self, client, mock_case_service, mock_case, headers):
        """Test case creation with metadata."""
        mock_case.metadata = {"source": "api"}
        mock_case_service.create_case.return_value = mock_case

        response = await client.post(
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

    async def test_create_case_missing_title(self, client, mock_case_service, mock_case, headers):
        """Test case creation without title - should auto-generate title (v2.0 behavior)."""
        # Configure mock to return case with auto-generated title
        mock_case.title = "Auto-generated Title: Test"
        mock_case_service.create_case.return_value = mock_case

        response = await client.post(
            "/api/v1/cases",
            json={
                "description": "Test",
                "severity": "low",
            },
            headers=headers,
        )

        # v2.0 API: Missing title is allowed, auto-generated by service
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["title"] is not None  # Should have auto-generated title
        assert len(data["title"]) > 0  # Title should not be empty
        # Verify create_case was called with title=None (for auto-generation)
        mock_case_service.create_case.assert_called_once()
        call_args = mock_case_service.create_case.call_args
        assert call_args.kwargs["title"] is None  # Should pass None for auto-generation

    async def test_create_case_empty_title(self, client, mock_case_service, mock_case, headers):
        """Test case creation with empty title - should auto-generate (v2.0 behavior)."""
        # Configure mock to return case with auto-generated title
        mock_case.title = "Auto-generated Title: Test"
        mock_case_service.create_case.return_value = mock_case

        response = await client.post(
            "/api/v1/cases",
            json={
                "title": "",
                "description": "Test",
                "severity": "medium",
            },
            headers=headers,
        )

        # v2.0 API: Empty title treated same as missing, auto-generated by service
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["title"] is not None
        assert len(data["title"]) > 0

    async def test_create_case_missing_authentication(self, mock_case_service, mock_session_service):
        """Test case creation without JWT authentication returns 401."""
        # Create app WITH service mocks but WITHOUT auth override
        # This allows dependencies to resolve but auth to fail naturally
        from faultmaven.main import app as main_app
        from httpx import AsyncClient, ASGITransport
        from faultmaven.modules.case.api.routes import _di_get_case_service_dependency, _di_get_session_service_dependency

        app = main_app

        # Mock ONLY the services, NOT the auth
        async def get_mock_case_service():
            return mock_case_service

        async def get_mock_session_service():
            return mock_session_service

        app.dependency_overrides[_di_get_case_service_dependency] = get_mock_case_service
        app.dependency_overrides[_di_get_session_service_dependency] = get_mock_session_service

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/cases",
                    json={
                        "title": "Test",
                        "description": "Test",
                        "severity": "medium",
                    },
                )

            assert response.status_code == status.HTTP_401_UNAUTHORIZED
        finally:
            # Clean up overrides
            app.dependency_overrides.clear()

    async def test_create_case_all_severities(self, client, mock_case_service, mock_case, headers):
        """Test case creation with all severity levels."""
        mock_case_service.create_case.return_value = mock_case

        for severity in ["low", "medium", "high", "critical"]:
            response = await client.post(
                "/api/v1/cases",
                json={
                    "title": "Test",
                    "description": "Test",
                    "severity": severity,
                },
                headers=headers,
            )
            assert response.status_code == status.HTTP_201_CREATED

    async def test_create_case_invalid_severity(self, client, headers):
        """Test case creation with invalid severity."""
        response = await client.post(
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

    async def test_get_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case retrieval."""
        mock_case_service.get_case.return_value = mock_case

        response = await client.get(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["case_id"] == "case_123abc"
        mock_case_service.get_case.assert_called_once_with(
            "case_123abc", "org_456"
        )

    async def test_get_case_not_found(self, client, mock_case_service, headers):
        """Test case not found."""
        mock_case_service.get_case.return_value = None

        response = await client.get(
            "/api/v1/cases/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        data = response.json()
        assert data["error"] == "Not Found"
        assert "nonexistent" in data["detail"]

    async def test_get_case_missing_authentication(self):
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

    async def test_list_cases_success(self, client, mock_case_service, mock_case_summary, headers):
        """Test successful case list."""
        mock_case_service.list_user_cases.return_value = [mock_case_summary]

        response = await client.get(
            "/api/v1/cases",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "cases" in data
        assert len(data["cases"]) == 1
        assert data["cases"][0]["case_id"] == "case_123abc"

    async def test_list_cases_empty(self, client, mock_case_service, headers):
        """Test empty case list."""
        mock_case_service.list_user_cases.return_value = []

        response = await client.get(
            "/api/v1/cases",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["cases"] == []
        assert data["total_count"] == 0

    async def test_list_cases_with_status_filter(self, client, mock_case_service, mock_case_summary, headers):
        """Test case list with status filter."""
        mock_case_service.list_user_cases.return_value = [mock_case_summary]

        response = await client.get(
            "/api/v1/cases?status=consulting",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.list_user_cases.assert_called_once()
        # Verify the filter object passed to list_user_cases has the status filter
        call_args = mock_case_service.list_user_cases.call_args
        filters = call_args[0][1] if len(call_args[0]) > 1 else call_args.kwargs.get("filters")
        assert filters.status == CaseStatus.CONSULTING

    async def test_list_cases_with_severity_filter(self, client, mock_case_service, mock_case_summary, headers):
        """Test case list with severity filter."""
        mock_case_service.list_user_cases.return_value = [mock_case_summary]

        response = await client.get(
            "/api/v1/cases?severity=high",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.list_user_cases.assert_called_once()
        # Note: severity filter may not be implemented in current API
        # If API doesn't support severity filter, this test needs updating

    async def test_list_cases_pagination(self, client, mock_case_service, headers):
        """Test case list pagination."""
        mock_case_service.list_user_cases.return_value = []

        response = await client.get(
            "/api/v1/cases?limit=10&offset=20",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 20

    async def test_list_cases_limit_validation(self, client, headers):
        """Test case list limit validation."""
        # Limit too high
        response = await client.get(
            "/api/v1/cases?limit=200",
            headers=headers,
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

        # Limit too low
        response = await client.get(
            "/api/v1/cases?limit=0",
            headers=headers,
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ============================================================
# PATCH /api/v1/cases/{case_id} Tests
# ============================================================


class TestUpdateCase:
    """Tests for PATCH /api/v1/cases/{case_id} endpoint."""

    async def test_update_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case update."""
        mock_case.title = "Updated Title"
        mock_case_service.update_case.return_value = mock_case

        response = await client.patch(
            "/api/v1/cases/case_123abc",
            json={"title": "Updated Title"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["title"] == "Updated Title"

    async def test_update_case_multiple_fields(self, client, mock_case_service, mock_case, headers):
        """Test updating multiple fields."""
        mock_case.title = "Updated"
        mock_case.status = CaseStatus.INVESTIGATING
        mock_case_service.update_case.return_value = mock_case

        response = await client.patch(
            "/api/v1/cases/case_123abc",
            json={
                "title": "Updated",
                "status": "investigating",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK

    async def test_update_case_not_found(self, client, mock_case_service, headers):
        """Test updating non-existent case."""
        from faultmaven.exceptions import NotFoundError
        mock_case_service.update_case.side_effect = NotFoundError("Case", "nonexistent")

        response = await client.patch(
            "/api/v1/cases/nonexistent",
            json={"title": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_update_case_forbidden(self, client, mock_case_service, headers):
        """Test updating case from different organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_case_service.update_case.side_effect = AuthorizationError(
            "Case not accessible by organization"
        )

        response = await client.patch(
            "/api/v1/cases/case_123abc",
            json={"title": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_update_case_empty_request(self, client, mock_case_service, mock_case, headers):
        """Test update with empty request body."""
        mock_case_service.get_case.return_value = mock_case

        response = await client.patch(
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

    async def test_delete_case_success(self, client, mock_case_service, headers):
        """Test successful case deletion."""
        mock_case_service.delete_case.return_value = True

        response = await client.delete(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT

    async def test_delete_case_not_found(self, client, mock_case_service, headers):
        """Test deleting non-existent case."""
        mock_case_service.delete_case.return_value = False

        response = await client.delete(
            "/api/v1/cases/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_case_forbidden(self, client, mock_case_service, headers):
        """Test deleting case from different organization."""
        from faultmaven.exceptions import AuthorizationError
        mock_case_service.delete_case.side_effect = AuthorizationError(
            "Not authorized"
        )

        response = await client.delete(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


# ============================================================
# POST /api/v1/cases/{case_id}/assign Tests
# ============================================================


class TestAssignCase:
    """Tests for POST /api/v1/cases/{case_id}/assign endpoint."""

    async def test_assign_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case assignment."""
        mock_case_service.assign_case.return_value = mock_case

        response = await client.post(
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

    async def test_assign_case_not_found(self, client, mock_case_service, headers):
        """Test assigning non-existent case."""
        from faultmaven.exceptions import NotFoundError
        mock_case_service.assign_case.side_effect = NotFoundError("Case", "nonexistent")

        response = await client.post(
            "/api/v1/cases/nonexistent/assign",
            json={"assigned_to": "user_assigned"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_assign_case_missing_assignee(self, client, headers):
        """Test assigning without assignee."""
        response = await client.post(
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

    async def test_close_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case closure."""
        mock_case.status = CaseStatus.RESOLVED
        mock_case_service.close_case.return_value = mock_case

        response = await client.post(
            "/api/v1/cases/case_123abc/close",
            json={"resolution": "Issue was fixed"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.close_case.assert_called_once()

    async def test_close_case_already_closed(self, client, mock_case_service, headers):
        """Test closing already closed case."""
        from faultmaven.exceptions import ConflictError
        mock_case_service.close_case.side_effect = ConflictError(
            "Case already closed",
            resource_type="Case",
            resource_id="case_123abc",
            conflict_reason="already_closed",
        )

        response = await client.post(
            "/api/v1/cases/case_123abc/close",
            json={"resolution": "Fixed"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_close_case_missing_resolution(self, client, headers):
        """Test closing without resolution."""
        response = await client.post(
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

    async def test_reopen_case_success(self, client, mock_case_service, mock_case, headers):
        """Test successful case reopening."""
        mock_case.status = CaseStatus.CONSULTING
        mock_case_service.reopen_case.return_value = mock_case

        response = await client.post(
            "/api/v1/cases/case_123abc/reopen",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "consulting"

    async def test_reopen_case_not_closed(self, client, mock_case_service, headers):
        """Test reopening case that's not closed."""
        from faultmaven.exceptions import ConflictError
        mock_case_service.reopen_case.side_effect = ConflictError(
            "Case not closed",
            resource_type="Case",
            resource_id="case_123abc",
            conflict_reason="not_closed",
        )

        response = await client.post(
            "/api/v1/cases/case_123abc/reopen",
            headers=headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT


# ============================================================
# GET /api/v1/cases/{case_id}/statistics Tests
# ============================================================


class TestCaseStatistics:
    """Tests for GET /api/v1/cases/{case_id}/statistics endpoint."""

    async def test_get_case_statistics_success(self, client, mock_case_service, mock_case, headers):
        """Test successful statistics retrieval."""
        mock_case_service.get_case.return_value = mock_case
        mock_case_service.get_case_with_details.return_value = {
            "case": mock_case,
            "sessions": [],
            "evidence": [],
            "executions": [],
        }

        response = await client.get(
            "/api/v1/cases/case_123abc/statistics",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "case_id" in data
        assert "session_count" in data
        assert "evidence_count" in data

    async def test_get_case_statistics_not_found(self, client, mock_case_service, headers):
        """Test statistics for non-existent case."""
        mock_case_service.get_case.return_value = None

        response = await client.get(
            "/api/v1/cases/nonexistent/statistics",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# Error Handling Tests
# ============================================================


class TestErrorHandling:
    """Tests for error handling in case endpoints."""

    async def test_service_error_returns_500(self, client, mock_case_service, headers):
        """Test that service errors return 500."""
        from faultmaven.exceptions import ServiceError
        mock_case_service.create_case.side_effect = ServiceError("Database error")

        response = await client.post(
            "/api/v1/cases",
            json={
                "title": "Test",
                "description": "Test",
                "severity": "medium",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    async def test_validation_error_returns_400(self, client, mock_case_service, headers):
        """Test that validation errors return 400."""
        from faultmaven.exceptions import ValidationException
        mock_case_service.create_case.side_effect = ValidationException(
            "title: Title is required"
        )

        response = await client.post(
            "/api/v1/cases",
            json={
                "title": "Test",
                "description": "Test",
                "severity": "medium",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
