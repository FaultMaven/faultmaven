"""Integration tests for Session Enhancement API endpoints (Phase 3 Week 19-20).

Tests microservices parity endpoints:
- PUT /api/v1/sessions/{session_id} - Update session
- POST /api/v1/sessions/search - Search sessions
- POST /api/v1/sessions/{session_id}/archive - Archive session

These endpoints provide microservices parity with fm-session-service while
maintaining the monolith's spec-compliant architecture (sessions are auth-only).
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import status

from faultmaven.main import app as main_app
from faultmaven.models.auth import DevUser
from faultmaven.models.common import SessionContext


@pytest.fixture
def mock_user():
    """Create a mock authenticated user."""
    return DevUser(
        id=uuid4(),
        user_id="test_user_123",
        username="testuser",
        email="test@example.com",
    )


@pytest.fixture
def sample_session():
    """Create a sample session for testing."""
    return SessionContext(
        session_id="session_abc123",
        user_id="test_user_123",
        client_id="client_xyz",
        created_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={
            "session_type": "troubleshooting",
            "timeout_minutes": 180,
            "status": "active"
        }
    )


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

    async def get_mock_user():
        return mock_user

    from faultmaven.api.v1.dependencies import get_session_service
    from faultmaven.api.v1.auth_dependencies import require_authentication

    app.dependency_overrides[get_session_service] = get_mock_session_service
    app.dependency_overrides[require_authentication] = get_mock_user

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


# =============================================================================
# PUT /api/v1/sessions/{session_id} - Update Session
# =============================================================================

def test_update_session_success(client, mock_session_service, sample_session):
    """Test successful session update."""
    # Setup
    mock_session_service.get_session.return_value = sample_session
    mock_session_service.update_session.return_value = True

    updated_session = SessionContext(
        session_id=sample_session.session_id,
        user_id=sample_session.user_id,
        client_id=sample_session.client_id,
        created_at=sample_session.created_at,
        last_activity=datetime.now(timezone.utc),
        metadata={
            "session_type": "troubleshooting",
            "timeout_minutes": 240,  # Updated
            "status": "active"
        }
    )
    mock_session_service.get_session.side_effect = [sample_session, updated_session]

    # Execute
    response = client.put(
        f"/api/v1/sessions/{sample_session.session_id}",
        json={"metadata": {"timeout_minutes": 240}}
    )

    # Assert
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["session_id"] == sample_session.session_id
    assert data["user_id"] == sample_session.user_id
    assert "updated_at" in data
    assert data["message"] == "Session updated successfully"


def test_update_session_not_found(client, mock_session_service):
    """Test update session when session doesn't exist."""
    # Setup
    mock_session_service.get_session.return_value = None

    # Execute
    response = client.put(
        "/api/v1/sessions/nonexistent_session",
        json={"metadata": {"timeout_minutes": 240}}
    )

    # Assert
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_update_session_unauthorized(client, mock_session_service, sample_session, mock_user):
    """Test update session when user doesn't own the session."""
    # Setup - session belongs to different user
    other_user_session = SessionContext(
        session_id="session_abc123",
        user_id="different_user",
        client_id="client_xyz",
        created_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={"status": "active"}
    )
    mock_session_service.get_session.return_value = other_user_session

    # Execute
    response = client.put(
        f"/api/v1/sessions/{other_user_session.session_id}",
        json={"metadata": {"timeout_minutes": 240}}
    )

    # Assert
    assert response.status_code == status.HTTP_403_FORBIDDEN


# =============================================================================
# POST /api/v1/sessions/search - Search Sessions
# =============================================================================

def test_search_sessions_success(client, mock_session_service, sample_session):
    """Test successful session search."""
    # Setup
    sessions = [
        sample_session,
        SessionContext(
            session_id="session_def456",
            user_id="test_user_123",
            client_id="client_abc",
            created_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            metadata={"status": "active", "session_type": "troubleshooting"}
        )
    ]
    mock_session_service.search_sessions.return_value = sessions

    # Execute
    response = client.post(
        "/api/v1/sessions/search",
        json={"status": "active", "limit": 50}
    )

    # Assert
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "sessions" in data
    assert "total" in data
    assert len(data["sessions"]) == 2
    assert data["total"] == 2
    assert data["sessions"][0]["session_id"] == sample_session.session_id


def test_search_sessions_with_query(client, mock_session_service, sample_session):
    """Test session search with text query."""
    # Setup
    mock_session_service.search_sessions.return_value = [sample_session]

    # Execute
    response = client.post(
        "/api/v1/sessions/search",
        json={"query": "troubleshooting", "limit": 50}
    )

    # Assert
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data["sessions"]) == 1
    mock_session_service.search_sessions.assert_called_once()
    call_kwargs = mock_session_service.search_sessions.call_args[1]
    assert call_kwargs["query"] == "troubleshooting"


def test_search_sessions_empty_results(client, mock_session_service):
    """Test session search with no matching results."""
    # Setup
    mock_session_service.search_sessions.return_value = []

    # Execute
    response = client.post(
        "/api/v1/sessions/search",
        json={"status": "archived", "limit": 50}
    )

    # Assert
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["sessions"] == []
    assert data["total"] == 0


# =============================================================================
# POST /api/v1/sessions/{session_id}/archive - Archive Session
# =============================================================================

def test_archive_session_success(client, mock_session_service, sample_session):
    """Test successful session archiving."""
    # Setup
    mock_session_service.get_session.return_value = sample_session
    mock_session_service.archive_session.return_value = True

    # Execute
    response = client.post(f"/api/v1/sessions/{sample_session.session_id}/archive")

    # Assert
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["session_id"] == sample_session.session_id
    assert data["status"] == "archived"
    assert data["message"] == "Session archived successfully"
    mock_session_service.archive_session.assert_called_once_with(sample_session.session_id)


def test_archive_session_not_found(client, mock_session_service):
    """Test archive session when session doesn't exist."""
    # Setup
    mock_session_service.get_session.return_value = None

    # Execute
    response = client.post("/api/v1/sessions/nonexistent_session/archive")

    # Assert
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_archive_session_unauthorized(client, mock_session_service, mock_user):
    """Test archive session when user doesn't own the session."""
    # Setup - session belongs to different user
    other_user_session = SessionContext(
        session_id="session_abc123",
        user_id="different_user",
        client_id="client_xyz",
        created_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        metadata={"status": "active"}
    )
    mock_session_service.get_session.return_value = other_user_session

    # Execute
    response = client.post(f"/api/v1/sessions/{other_user_session.session_id}/archive")

    # Assert
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_archive_session_failure(client, mock_session_service, sample_session):
    """Test archive session when archiving fails."""
    # Setup
    mock_session_service.get_session.return_value = sample_session
    mock_session_service.archive_session.return_value = False

    # Execute
    response = client.post(f"/api/v1/sessions/{sample_session.session_id}/archive")

    # Assert
    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
