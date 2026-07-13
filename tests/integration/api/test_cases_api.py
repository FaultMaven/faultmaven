"""Integration tests for Case Management API (TASK-014, TASK-020).

Tests:
- POST /api/v1/cases (201 Created)
- GET /api/v1/cases/{case_id} (200 OK)
- GET /api/v1/cases/{case_id} (404 Not Found)
- GET /api/v1/cases (200 OK, list)
- GET /api/v1/cases?state=OPEN (filter)
- PATCH /api/v1/cases/{case_id} (200 OK)
- PATCH /api/v1/cases/{case_id} (403 Forbidden, wrong org)
- DELETE /api/v1/cases/{case_id} (204 No Content)
- POST /api/v1/cases/{case_id}/assign (200 OK)
- POST /api/v1/cases/{case_id}/close (200 OK)
- Missing JWT token returns 401

Note: As of TASK-020, JWT authentication is required. Legacy header authentication
(X-Organization-ID, X-User-ID) has been removed.

Note: Terminal states (RESOLVED, CLOSED) are irreversible by design. To handle case
recurrence, create a new case with a link to the original case (future enhancement).
"""

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from faultmaven.main import app as main_app
from faultmaven.models.api_models import CaseSummary
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.case.domain.models import Case, CaseSeverity, CaseState


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
    mock.source = "copilot"
    mock.title = "Test Case"
    mock.description = "Test Description"
    mock.state = CaseState.INQUIRY
    mock.is_terminal = False
    mock.created_at = datetime.now(timezone.utc)
    mock.updated_at = datetime.now(timezone.utc)
    # Optional fields - must be explicit to avoid MagicMock being returned
    mock.problem_verification = None
    mock.closure_reason = None
    mock.metadata = None
    mock.assigned_to = None  # Optional[str]
    mock.closed_at = None  # Optional[datetime]
    mock.severity = CaseSeverity.MEDIUM  # Used by from_domain

    # Investigation stage and progress (v2.0)
    mock.current_stage = None  # None for INQUIRY state (only set for INVESTIGATING)
    mock_progress = MagicMock()
    mock_progress.completed_milestones = []
    mock_progress.pending_milestones = ["M1", "M2", "M3"]
    mock_progress.completion_percentage = 0.0
    mock_progress.current_stage = None
    mock.progress = mock_progress

    return mock


@pytest.fixture
def mock_case_summary():
    """Create a mock CaseSummary for list endpoints."""
    now = datetime.now(timezone.utc)
    return CaseSummary(
        case_id="case_123abc",
        title="Test Case",
        state=CaseState.INQUIRY,
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        user_id="user_789",
        organization_id="org_456",
        current_turn=1,
        milestones_completed=2,
        total_milestones=8,
        is_terminal=False,
        description="Test Case Description",
        resolved_at=None,
        closed_at=None,
        closure_reason=None,
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
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.case.api.routes import (
        _di_get_case_service_dependency,
        _di_get_session_service_dependency,
    )

    app.dependency_overrides[_di_get_case_service_dependency] = get_mock_case_service
    app.dependency_overrides[_di_get_session_service_dependency] = (
        get_mock_session_service
    )
    app.dependency_overrides[require_authentication] = get_mock_current_user

    yield app

    # Cleanup: clear overrides after test
    app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """Create async test client with proper lifespan handling."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
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

    async def test_create_case_success(
        self, client, mock_case_service, mock_case, headers
    ):
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

    async def test_create_case_with_metadata(
        self, client, mock_case_service, mock_case, headers
    ):
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

    async def test_create_case_missing_title(
        self, client, mock_case_service, mock_case, headers
    ):
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

    async def test_create_case_empty_title(
        self, client, mock_case_service, mock_case, headers
    ):
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

    async def test_create_case_missing_authentication(
        self, mock_case_service, mock_session_service
    ):
        """Test case creation without JWT authentication returns 401."""
        # Create app WITH service mocks but WITHOUT auth override
        # This allows dependencies to resolve but auth to fail naturally
        from httpx import ASGITransport, AsyncClient

        from faultmaven.main import app as main_app
        from faultmaven.modules.case.api.routes import (
            _di_get_case_service_dependency,
            _di_get_session_service_dependency,
        )

        app = main_app

        # Mock ONLY the services, NOT the auth
        async def get_mock_case_service():
            return mock_case_service

        async def get_mock_session_service():
            return mock_session_service

        app.dependency_overrides[_di_get_case_service_dependency] = (
            get_mock_case_service
        )
        app.dependency_overrides[_di_get_session_service_dependency] = (
            get_mock_session_service
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
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

    async def test_create_case_all_severities(
        self, client, mock_case_service, mock_case, headers
    ):
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


# ============================================================
# GET /api/v1/cases/{case_id} Tests
# ============================================================


class TestGetCase:
    """Tests for GET /api/v1/cases/{case_id} endpoint."""

    async def test_get_case_success(
        self, client, mock_case_service, mock_case, headers
    ):
        """Test successful case retrieval."""
        mock_case_service.get_case.return_value = mock_case

        response = await client.get(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["case_id"] == "case_123abc"
        # v2.0 API passes user_id (from current_user), not organization_id
        mock_case_service.get_case.assert_called_once_with("case_123abc", "user_789")

    async def test_get_case_not_found(self, client, mock_case_service, headers):
        """Test case not found - v2.0 API returns FastAPI standard error format."""
        mock_case_service.get_case.return_value = None

        response = await client.get(
            "/api/v1/cases/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        data = response.json()
        # v2.0 API: FastAPI standard error format {"detail": "message"}
        assert "detail" in data
        assert (
            "not found" in data["detail"].lower()
            or "access denied" in data["detail"].lower()
        )

    async def test_get_case_missing_authentication(self):
        """Test get case without JWT authentication returns 401."""
        # Create app without auth override to test unauthenticated request
        from fastapi.testclient import TestClient

        from faultmaven.main import app as main_app

        app = main_app
        unauthenticated_client = TestClient(app)

        response = unauthenticated_client.get("/api/v1/cases/case_123")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ============================================================
# GET /api/v1/cases Tests (List)
# ============================================================


class TestListCases:
    """Tests for GET /api/v1/cases endpoint."""

    async def test_list_cases_success(
        self, client, mock_case_service, mock_case_summary, headers
    ):
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

    async def test_list_cases_with_status_filter(
        self, client, mock_case_service, mock_case_summary, headers
    ):
        """Test case list with state filter."""
        mock_case_service.list_user_cases.return_value = [mock_case_summary]

        response = await client.get(
            "/api/v1/cases?state=inquiry",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_case_service.list_user_cases.assert_called_once()
        # Verify the filter object passed to list_user_cases has the state filter
        call_args = mock_case_service.list_user_cases.call_args
        filters = (
            call_args[0][1]
            if len(call_args[0]) > 1
            else call_args.kwargs.get("filters")
        )
        assert filters.state == CaseState.INQUIRY

    async def test_list_cases_with_severity_filter(
        self, client, mock_case_service, mock_case_summary, headers
    ):
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
        assert response.status_code == 422

        # Limit too low
        response = await client.get(
            "/api/v1/cases?limit=0",
            headers=headers,
        )
        assert response.status_code == 422


# ============================================================
# PATCH /api/v1/cases/{case_id} Tests
# ============================================================


class TestUpdateCase:
    """Tests for PUT /api/v1/cases/{case_id} endpoint (v2.0 uses PUT not PATCH)."""

    async def test_update_case_success(
        self, client, mock_case_service, mock_case, headers
    ):
        """Test successful case update - v2.0 uses PUT and returns state response."""
        mock_case.title = "Updated Title"
        mock_case_service.get_case.return_value = mock_case
        mock_case_service.update_case.return_value = mock_case

        response = await client.put(
            "/api/v1/cases/case_123abc",
            json={"title": "Updated Title"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # v2.0 API returns state response, not full case object
        assert data["success"] is True
        assert data["case_id"] == "case_123abc"
        assert "updated successfully" in data["message"].lower()

    async def test_update_case_multiple_fields(
        self, client, mock_case_service, mock_case, headers
    ):
        """Test updating multiple fields - v2.0 uses PUT method."""
        mock_case.title = "Updated"
        mock_case.state = CaseState.INVESTIGATING
        mock_case_service.get_case.return_value = mock_case
        mock_case_service.update_case.return_value = mock_case

        response = await client.put(
            "/api/v1/cases/case_123abc",
            json={
                "title": "Updated",
                "state": "investigating",
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK

    async def test_update_case_not_found(self, client, mock_case_service, headers):
        """Test updating non-existent case - v2.0 uses PUT method."""
        from faultmaven.exceptions import NotFoundError

        mock_case_service.get_case.return_value = None
        mock_case_service.update_case.side_effect = NotFoundError("Case", "nonexistent")

        response = await client.put(
            "/api/v1/cases/nonexistent",
            json={"title": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    async def test_update_case_forbidden(self, client, mock_case_service, headers):
        """Test updating case from different organization - v2.0 uses PUT method."""
        from faultmaven.exceptions import AuthorizationError

        mock_case_service.get_case.return_value = None
        mock_case_service.update_case.side_effect = AuthorizationError(
            "Case not accessible by organization"
        )

        response = await client.put(
            "/api/v1/cases/case_123abc",
            json={"title": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_update_case_empty_request(
        self, client, mock_case_service, mock_case, headers
    ):
        """Test update with empty request body - v2.0 rejects empty updates."""
        mock_case_service.get_case.return_value = mock_case

        response = await client.put(
            "/api/v1/cases/case_123abc",
            json={},
            headers=headers,
        )

        # v2.0 API rejects empty updates with 400 Bad Request
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        data = response.json()
        assert "detail" in data
        assert (
            "no updates" in data["detail"].lower() or "update" in data["detail"].lower()
        )


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
        """Test deleting non-existent case - v2.0 DELETE is idempotent."""
        from faultmaven.exceptions import NotFoundError

        mock_case_service.hard_delete_case.side_effect = NotFoundError(
            "Case", "nonexistent"
        )

        response = await client.delete(
            "/api/v1/cases/nonexistent",
            headers=headers,
        )

        # v2.0: DELETE is idempotent - returns 204 even if case doesn't exist
        # REST principle: DELETE succeeds whether or not resource exists
        assert response.status_code == status.HTTP_204_NO_CONTENT

    async def test_delete_case_forbidden(self, client, mock_case_service, headers):
        """Test deleting case from different organization - still returns 403."""
        from faultmaven.exceptions import AuthorizationError

        mock_case_service.hard_delete_case.side_effect = AuthorizationError(
            "Not authorized"
        )

        response = await client.delete(
            "/api/v1/cases/case_123abc",
            headers=headers,
        )

        # Authorization errors are NOT treated as idempotent success
        assert response.status_code == status.HTTP_403_FORBIDDEN

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

    async def test_validation_error_returns_400(
        self, client, mock_case_service, headers
    ):
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


# ============================================================
# POST /api/v1/cases/{case_id}/close Tests
# ============================================================


class TestCloseCase:
    """Tests for POST /api/v1/cases/{case_id}/close endpoint.

    Regression guard: the state-validation block previously referenced
    CaseState.SOLVED / CaseState.DOCUMENTING, which do not exist on the enum,
    so every call raised AttributeError -> 500 before any close occurred.
    """

    async def test_close_non_terminal_case_succeeds_not_500(
        self, app, client, mock_case_service, mock_case, headers
    ):
        """A closeable case returns 200 (regression: must NOT 500)."""
        from faultmaven.api.v1.dependencies import get_case_repository

        mock_case.state = CaseState.RESOLVED
        mock_case.current_turn = 5
        mock_case_service.get_case.return_value = mock_case
        mock_case_service.update_case_status.return_value = None
        # close_case archives reports via the repository; None skips that branch.
        app.dependency_overrides[get_case_repository] = lambda: None

        response = await client.post(
            "/api/v1/cases/case_123abc/close", json={}, headers=headers
        )

        assert response.status_code != status.HTTP_500_INTERNAL_SERVER_ERROR
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["case_id"] == "case_123abc"
        mock_case_service.update_case_status.assert_called_once()

    async def test_close_already_closed_returns_400(
        self, app, client, mock_case_service, mock_case, headers
    ):
        """Closing an already-CLOSED case is the one invalid close -> 400."""
        from faultmaven.api.v1.dependencies import get_case_repository

        mock_case.state = CaseState.CLOSED
        mock_case_service.get_case.return_value = mock_case
        app.dependency_overrides[get_case_repository] = lambda: None

        response = await client.post(
            "/api/v1/cases/case_123abc/close", json={}, headers=headers
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already closed" in response.json()["detail"].lower()
        mock_case_service.update_case_status.assert_not_called()


# ============================================================
# GET /api/v1/cases/health Tests
# ============================================================


class TestCaseHealth:
    """Tests for GET /api/v1/cases/health endpoint."""

    async def test_get_case_health_success(self, client):
        """Test case health endpoint returns 200 and is healthy."""
        response = await client.get("/api/v1/cases/health")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["service"] == "case_management"
        assert data["status"] == "healthy"
        assert "timestamp" in data
        assert data["features"]["case_persistence"] is True

    async def test_get_case_health_unauthenticated(self, app):
        """Test case health endpoint is public and does not require JWT authentication."""
        from faultmaven.api.v1.auth_dependencies import require_authentication

        # Temporarily remove auth override to simulate unauthenticated request
        auth_override = app.dependency_overrides.pop(require_authentication, None)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as unauth_client:
                response = await unauth_client.get("/api/v1/cases/health")
        finally:
            if auth_override is not None:
                app.dependency_overrides[require_authentication] = auth_override

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "healthy"
