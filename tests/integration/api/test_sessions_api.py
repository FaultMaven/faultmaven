"""Integration tests for Investigation Session API (TASK-014, TASK-020).

Tests:
- POST /api/v1/cases/{case_id}/sessions (201 Created)
- GET /api/v1/cases/{case_id}/sessions/{session_id} (200 OK)
- GET /api/v1/cases/{case_id}/sessions (200 OK, list)
- PATCH /api/v1/cases/{case_id}/sessions/{session_id} (200 OK)
- POST /api/v1/cases/{case_id}/sessions/{session_id}/pause (200 OK)
- POST /api/v1/cases/{case_id}/sessions/{session_id}/resume (200 OK)
- POST /api/v1/cases/{case_id}/sessions/{session_id}/complete (200 OK)
- GET /api/v1/cases/{case_id}/sessions/active (200 OK)
- Authorization errors (403 Forbidden)
- Missing JWT token returns 401

Note: As of TASK-020, JWT authentication is required. Legacy header authentication
(X-Organization-ID, X-User-ID) has been removed.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.models.investigation_session import SessionStatus


@pytest.fixture
def mock_user():
    """Create a mock authenticated user for testing."""
    return AuthenticatedUser(
        user_id="user_789",
        organization_id="org_456",
        email="test@example.com",
        roles=["admin"],
        permissions=["sessions:read", "sessions:write", "sessions:execute"],
    )


@pytest.fixture
def mock_session():
    """Create a mock InvestigationSession for testing."""
    mock = MagicMock()
    mock.session_id = "session_123abc"
    mock.case_id = "case_456def"
    mock.user_id = "user_789"
    mock.organization_id = "org_456"
    mock.status = SessionStatus.ACTIVE
    mock.started_at = datetime.now(timezone.utc)
    mock.ended_at = None
    mock.last_activity_at = datetime.now(timezone.utc)
    mock.total_duration_ms = None
    mock.session_goal = "Investigate the issue"
    mock.findings_summary = None
    mock.total_token_usage = 1000
    mock.total_agent_executions = 5
    mock.token_budget_limit = 50000
    mock.created_at = datetime.now(timezone.utc)
    mock.updated_at = datetime.now(timezone.utc)
    return mock


@pytest.fixture
def mock_session_service():
    """Create a mock session service."""
    service = AsyncMock()
    return service


@pytest.fixture
def app(mock_session_service, mock_user):
    """Create test application with mocked dependencies."""
    app = main_app

    async def get_mock_session_service():
        return mock_session_service

    async def get_mock_current_user():
        return mock_user

    from faultmaven.api.dependencies import get_investigation_session_service
    from faultmaven.api.middleware.auth import get_current_user

    app.dependency_overrides[get_investigation_session_service] = (
        get_mock_session_service
    )
    app.dependency_overrides[get_current_user] = get_mock_current_user

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def headers():
    """Standard request headers (auth is mocked via dependency override)."""
    return {}


# ============================================================
# POST /api/v1/cases/{case_id}/sessions Tests
# ============================================================


class TestCreateSession:
    """Tests for POST /api/v1/cases/{case_id}/sessions endpoint."""

    def test_create_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session creation."""
        mock_session_service.create_session.return_value = mock_session

        response = client.post(
            "/api/v1/cases/case_456def/sessions",
            json={"session_goal": "Investigate the issue"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["session_id"] == "session_123abc"
        assert data["status"] == "active"

    def test_create_session_with_budget(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test session creation with token budget."""
        mock_session_service.create_session.return_value = mock_session

        response = client.post(
            "/api/v1/cases/case_456def/sessions",
            json={
                "session_goal": "Investigate",
                "token_budget_limit": 10000,
            },
            headers=headers,
        )

        assert response.status_code == status.HTTP_201_CREATED
        mock_session_service.create_session.assert_called_once()

    def test_create_session_minimal(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test session creation with minimal data."""
        mock_session_service.create_session.return_value = mock_session

        response = client.post(
            "/api/v1/cases/case_456def/sessions",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_201_CREATED

    def test_create_session_case_not_found(self, client, mock_session_service, headers):
        """Test session creation for non-existent case."""
        from faultmaven.exceptions import NotFoundError

        mock_session_service.create_session.side_effect = NotFoundError(
            "Case", "nonexistent"
        )

        response = client.post(
            "/api/v1/cases/nonexistent/sessions",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_create_session_active_exists(self, client, mock_session_service, headers):
        """Test session creation when active session exists."""
        from faultmaven.exceptions import ConflictError

        mock_session_service.create_session.side_effect = ConflictError(
            "Active session already exists",
            resource_type="Session",
            resource_id="existing_session",
            conflict_reason="active_session_exists",
        )

        response = client.post(
            "/api/v1/cases/case_456def/sessions",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_409_CONFLICT

    def test_create_session_negative_budget(self, client, headers):
        """Test session creation with negative budget."""
        response = client.post(
            "/api/v1/cases/case_456def/sessions",
            json={"token_budget_limit": -100},
            headers=headers,
        )

        assert response.status_code == 422

    # Note: test_create_session_missing_headers deleted - obsolete for v2.0 JWT auth
    # v2.0 uses JWT authentication which is mocked in fixtures, so "missing headers"
    # cannot be tested in this integration test setup. JWT authentication failure
    # (401) is tested in auth middleware tests.


# ============================================================
# GET /api/v1/cases/{case_id}/sessions/{session_id} Tests
# ============================================================


class TestGetSession:
    """Tests for GET /api/v1/cases/{case_id}/sessions/{session_id} endpoint."""

    def test_get_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session retrieval."""
        mock_session_service.get_session.return_value = mock_session

        response = client.get(
            "/api/v1/cases/case_456def/sessions/session_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["session_id"] == "session_123abc"
        assert data["status"] == "active"

    def test_get_session_not_found(self, client, mock_session_service, headers):
        """Test session not found."""
        mock_session_service.get_session.return_value = None

        response = client.get(
            "/api/v1/cases/case_456def/sessions/nonexistent",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_get_session_wrong_case(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test session retrieval with wrong case ID."""
        mock_session.case_id = "different_case"
        mock_session_service.get_session.return_value = mock_session

        response = client.get(
            "/api/v1/cases/case_456def/sessions/session_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# GET /api/v1/cases/{case_id}/sessions Tests (List)
# ============================================================


class TestListSessions:
    """Tests for GET /api/v1/cases/{case_id}/sessions endpoint."""

    def test_list_sessions_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session list."""
        mock_session_service.list_sessions.return_value = [mock_session]

        response = client.get(
            "/api/v1/cases/case_456def/sessions",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data) == 1
        assert data[0]["session_id"] == "session_123abc"

    def test_list_sessions_empty(self, client, mock_session_service, headers):
        """Test empty session list."""
        mock_session_service.list_sessions.return_value = []

        response = client.get(
            "/api/v1/cases/case_456def/sessions",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data == []

    def test_list_sessions_with_status_filter(
        self, client, mock_session_service, headers
    ):
        """Test session list with status filter."""
        mock_session_service.list_sessions.return_value = []

        response = client.get(
            "/api/v1/cases/case_456def/sessions?status=paused",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        mock_session_service.list_sessions.assert_called_once()
        call_kwargs = mock_session_service.list_sessions.call_args.kwargs
        assert call_kwargs["status"] == SessionStatus.PAUSED

    def test_list_sessions_case_not_found(self, client, mock_session_service, headers):
        """Test listing sessions for non-existent case."""
        from faultmaven.exceptions import NotFoundError

        mock_session_service.list_sessions.side_effect = NotFoundError(
            "Case", "nonexistent"
        )

        response = client.get(
            "/api/v1/cases/nonexistent/sessions",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_sessions_pagination(self, client, mock_session_service, headers):
        """Test session list pagination parameters."""
        mock_session_service.list_sessions.return_value = []

        response = client.get(
            "/api/v1/cases/case_456def/sessions?limit=25&offset=50",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        call_kwargs = mock_session_service.list_sessions.call_args.kwargs
        assert call_kwargs["limit"] == 25
        assert call_kwargs["offset"] == 50


# ============================================================
# PATCH /api/v1/cases/{case_id}/sessions/{session_id} Tests
# ============================================================


class TestUpdateSession:
    """Tests for PATCH /api/v1/cases/{case_id}/sessions/{session_id} endpoint."""

    def test_update_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session update."""
        mock_session.session_goal = "Updated goal"
        mock_session_service.update_session.return_value = mock_session

        response = client.patch(
            "/api/v1/cases/case_456def/sessions/session_123abc",
            json={"session_goal": "Updated goal"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["session_goal"] == "Updated goal"

    def test_update_session_budget(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test updating session budget."""
        mock_session.token_budget_limit = 25000
        mock_session_service.update_session.return_value = mock_session

        response = client.patch(
            "/api/v1/cases/case_456def/sessions/session_123abc",
            json={"token_budget_limit": 25000},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK

    def test_update_session_not_found(self, client, mock_session_service, headers):
        """Test updating non-existent session."""
        from faultmaven.exceptions import NotFoundError

        mock_session_service.update_session.side_effect = NotFoundError(
            "Session", "nonexistent"
        )

        response = client.patch(
            "/api/v1/cases/case_456def/sessions/nonexistent",
            json={"session_goal": "Updated"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_update_session_empty_request(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test update with empty request."""
        mock_session_service.get_session.return_value = mock_session

        response = client.patch(
            "/api/v1/cases/case_456def/sessions/session_123abc",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK


# ============================================================
# POST /api/v1/cases/{case_id}/sessions/{session_id}/pause Tests
# ============================================================


class TestPauseSession:
    """Tests for POST /api/v1/cases/{case_id}/sessions/{session_id}/pause endpoint."""

    def test_pause_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session pause."""
        mock_session.status = SessionStatus.PAUSED
        mock_session_service.pause_session.return_value = mock_session

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/pause",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "paused"

    def test_pause_session_not_active(self, client, mock_session_service, headers):
        """Test pausing session that's not active."""
        from faultmaven.exceptions import ValidationException

        mock_session_service.pause_session.side_effect = ValidationException(
            "Cannot pause session in paused status"
        )

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/pause",
            headers=headers,
        )

        # v2.0: ValidationException returns 422 Unprocessable Entity (state validation error)
        assert response.status_code == 422

    def test_pause_session_not_found(self, client, mock_session_service, headers):
        """Test pausing non-existent session."""
        from faultmaven.exceptions import NotFoundError

        mock_session_service.pause_session.side_effect = NotFoundError(
            "Session", "nonexistent"
        )

        response = client.post(
            "/api/v1/cases/case_456def/sessions/nonexistent/pause",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# POST /api/v1/cases/{case_id}/sessions/{session_id}/resume Tests
# ============================================================


class TestResumeSession:
    """Tests for POST /api/v1/cases/{case_id}/sessions/{session_id}/resume endpoint."""

    def test_resume_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session resume."""
        mock_session.status = SessionStatus.ACTIVE
        mock_session_service.resume_session.return_value = mock_session

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/resume",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "active"

    def test_resume_session_not_paused(self, client, mock_session_service, headers):
        """Test resuming session that's not paused."""
        from faultmaven.exceptions import ValidationException

        mock_session_service.resume_session.side_effect = ValidationException(
            "Cannot resume session in active status"
        )

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/resume",
            headers=headers,
        )

        # v2.0: ValidationException returns 422 Unprocessable Entity (state validation error)
        assert response.status_code == 422


# ============================================================
# POST /api/v1/cases/{case_id}/sessions/{session_id}/complete Tests
# ============================================================


class TestCompleteSession:
    """Tests for POST /api/v1/cases/{case_id}/sessions/{session_id}/complete endpoint."""

    def test_complete_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful session completion."""
        mock_session.status = SessionStatus.COMPLETED
        mock_session.findings_summary = "Root cause identified"
        mock_session_service.complete_session.return_value = mock_session

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/complete",
            json={"findings_summary": "Root cause identified"},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "completed"
        assert data["findings_summary"] == "Root cause identified"

    def test_complete_session_missing_findings(self, client, headers):
        """Test completing session without findings."""
        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/complete",
            json={},
            headers=headers,
        )

        assert response.status_code == 422

    def test_complete_session_already_completed(
        self, client, mock_session_service, headers
    ):
        """Test completing already completed session."""
        from faultmaven.exceptions import ValidationException

        mock_session_service.complete_session.side_effect = ValidationException(
            "Cannot complete session in completed status"
        )

        response = client.post(
            "/api/v1/cases/case_456def/sessions/session_123abc/complete",
            json={"findings_summary": "Findings"},
            headers=headers,
        )

        # v2.0: ValidationException returns 422 Unprocessable Entity (state validation error)
        assert response.status_code == 422


# ============================================================
# GET /api/v1/cases/{case_id}/sessions/active Tests
# ============================================================


class TestGetActiveSession:
    """Tests for GET /api/v1/cases/{case_id}/sessions/active endpoint."""

    def test_get_active_session_success(
        self, client, mock_session_service, mock_session, headers
    ):
        """Test successful active session retrieval."""
        mock_session_service.get_active_session.return_value = mock_session

        response = client.get(
            "/api/v1/cases/case_456def/sessions/active",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["session_id"] == "session_123abc"
        assert data["status"] == "active"

    def test_get_active_session_none(self, client, mock_session_service, headers):
        """Test when no active session exists."""
        mock_session_service.get_active_session.return_value = None

        response = client.get(
            "/api/v1/cases/case_456def/sessions/active",
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json() is None

    def test_get_active_session_case_not_found(
        self, client, mock_session_service, headers
    ):
        """Test getting active session for non-existent case."""
        from faultmaven.exceptions import NotFoundError

        mock_session_service.get_active_session.side_effect = NotFoundError(
            "Case", "nonexistent"
        )

        response = client.get(
            "/api/v1/cases/nonexistent/sessions/active",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND


# ============================================================
# Authorization Tests
# ============================================================


class TestSessionAuthorization:
    """Tests for session authorization."""

    def test_create_session_forbidden(self, client, mock_session_service, headers):
        """Test session creation with wrong organization."""
        from faultmaven.exceptions import AuthorizationError

        mock_session_service.create_session.side_effect = AuthorizationError(
            "Case not accessible by organization"
        )

        response = client.post(
            "/api/v1/cases/case_456def/sessions",
            json={},
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_get_session_forbidden(self, client, mock_session_service, headers):
        """Test session retrieval with wrong organization."""
        mock_session_service.get_session.return_value = (
            None  # Unauthorized returns None
        )

        response = client.get(
            "/api/v1/cases/case_456def/sessions/session_123abc",
            headers=headers,
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_sessions_forbidden(self, client, mock_session_service, headers):
        """Test session list with wrong organization."""
        from faultmaven.exceptions import AuthorizationError

        mock_session_service.list_sessions.side_effect = AuthorizationError(
            "Not authorized"
        )

        response = client.get(
            "/api/v1/cases/case_456def/sessions",
            headers=headers,
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
