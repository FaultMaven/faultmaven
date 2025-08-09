from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.routes.session import router
from faultmaven.api.v1.dependencies import get_session_service

app = FastAPI()
app.include_router(router)


@pytest.fixture
def mock_session_service():
    """Fixture to mock the SessionService dependency."""
    mock = MagicMock()
    mock.create_session = AsyncMock()
    mock.get_session = AsyncMock()
    mock.list_sessions = AsyncMock()
    mock.delete_session = AsyncMock()
    mock.update_last_activity = AsyncMock()
    mock.get_session_stats = AsyncMock()
    app.dependency_overrides[get_session_service] = lambda: mock
    yield mock
    # Clean up dependency override
    app.dependency_overrides.pop(get_session_service, None)


def test_create_session_failure(mock_session_service):
    """
    Test session creation when the session service fails.
    """
    mock_session_service.create_session.side_effect = Exception(
        "Session creation failed"
    )

    with TestClient(app) as client:
        response = client.post("/sessions/")

    assert response.status_code == 500
    assert "Failed to create session" in response.json()["detail"]


def test_get_session_not_found(mock_session_service):
    """
    Test retrieving a session that does not exist.
    """
    mock_session_service.get_session.return_value = None

    with TestClient(app) as client:
        response = client.get("/sessions/non_existent_session")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_get_session_failure(mock_session_service):
    """
    Test retrieving a session when the session service fails.
    """
    mock_session_service.get_session.side_effect = Exception("Session retrieval failed")

    with TestClient(app) as client:
        response = client.get("/sessions/any_session_id")

    assert response.status_code == 500
    assert "Failed to get session" in response.json()["detail"]


def test_list_sessions_failure(mock_session_service):
    """
    Test listing sessions when the session service fails.
    """
    mock_session_service.list_sessions.side_effect = Exception("Session listing failed")

    with TestClient(app) as client:
        response = client.get("/sessions/")

    assert response.status_code == 500
    assert "Failed to list sessions" in response.json()["detail"]


def test_delete_session_not_found(mock_session_service):
    """
    Test deleting a session that does not exist.
    """
    # Mock get_session to return None (session doesn't exist)
    mock_session_service.get_session.return_value = None

    with TestClient(app) as client:
        response = client.delete("/sessions/non_existent_session")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_delete_session_failure(mock_session_service):
    """
    Test deleting a session when the session service fails.
    """
    mock_session_service.delete_session.side_effect = Exception(
        "Session deletion failed"
    )

    with TestClient(app) as client:
        response = client.delete("/sessions/any_session_id")

    assert response.status_code == 500
    assert "Failed to delete session" in response.json()["detail"]


def test_session_heartbeat_not_found(mock_session_service):
    """
    Test session heartbeat for a session that does not exist.
    """
    mock_session_service.update_last_activity.return_value = False

    with TestClient(app) as client:
        response = client.post("/sessions/non_existent_session/heartbeat")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_session_heartbeat_failure(mock_session_service):
    """
    Test session heartbeat when the session service fails.
    """
    mock_session_service.update_last_activity.side_effect = Exception("Session heartbeat failed")

    with TestClient(app) as client:
        response = client.post("/sessions/any_session_id/heartbeat")

    assert response.status_code == 500
    assert "Failed to update heartbeat" in response.json()["detail"]


def test_get_session_stats_not_found(mock_session_service):
    """
    Test getting stats for a session that does not exist.
    """
    # Mock get_session to return None (session doesn't exist)
    mock_session_service.get_session.return_value = None

    with TestClient(app) as client:
        response = client.get("/sessions/non_existent_session/stats")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_get_session_stats_failure(mock_session_service):
    """
    Test getting session stats when the session service fails.
    """
    # Mock get_session to fail with exception
    mock_session_service.get_session.side_effect = Exception("Session stats failed")

    with TestClient(app) as client:
        response = client.get("/sessions/any_session_id/stats")

    assert response.status_code == 500
    assert "Failed to get session stats" in response.json()["detail"]
