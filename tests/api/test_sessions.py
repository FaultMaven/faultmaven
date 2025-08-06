from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.routes.session import router, get_session_manager

app = FastAPI()
app.include_router(router)
client = TestClient(app)


@pytest.fixture
def mock_session_manager():
    """Fixture to mock the SessionManager dependency."""
    mock = MagicMock()
    mock.create_session = AsyncMock()
    mock.get_session = AsyncMock()
    mock.list_sessions = AsyncMock()
    mock.delete_session = AsyncMock()
    mock.update_session = AsyncMock()
    mock.get_session_stats = AsyncMock()
    app.dependency_overrides[get_session_manager] = lambda: mock
    yield mock
    # Clean up dependency override
    app.dependency_overrides.pop(get_session_manager, None)


def test_create_session_failure(mock_session_manager):
    """
    Test session creation when the session manager fails.
    """
    mock_session_manager.create_session.side_effect = Exception(
        "Session creation failed"
    )

    response = client.post("/sessions/")

    assert response.status_code == 500
    assert "Failed to create session" in response.json()["detail"]


def test_get_session_not_found(mock_session_manager):
    """
    Test retrieving a session that does not exist.
    """
    mock_session_manager.get_session.return_value = None

    response = client.get("/sessions/non_existent_session")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_get_session_failure(mock_session_manager):
    """
    Test retrieving a session when the session manager fails.
    """
    mock_session_manager.get_session.side_effect = Exception("Session retrieval failed")

    response = client.get("/sessions/any_session_id")

    assert response.status_code == 500
    assert "Failed to get session" in response.json()["detail"]


def test_list_sessions_failure(mock_session_manager):
    """
    Test listing sessions when the session manager fails.
    """
    mock_session_manager.list_sessions.side_effect = Exception("Session listing failed")

    response = client.get("/sessions/")

    assert response.status_code == 500
    assert "Failed to list sessions" in response.json()["detail"]


def test_delete_session_not_found(mock_session_manager):
    """
    Test deleting a session that does not exist.
    """
    mock_session_manager.get_session.return_value = None

    response = client.delete("/sessions/non_existent_session")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_delete_session_failure(mock_session_manager):
    """
    Test deleting a session when the session manager fails.
    """
    # Mock that session exists
    mock_session = MagicMock()
    mock_session_manager.get_session.return_value = mock_session
    mock_session_manager.delete_session.side_effect = Exception(
        "Session deletion failed"
    )

    response = client.delete("/sessions/any_session_id")

    assert response.status_code == 500
    assert "Failed to delete session" in response.json()["detail"]


def test_session_heartbeat_not_found(mock_session_manager):
    """
    Test session heartbeat for a session that does not exist.
    """
    mock_session_manager.get_session.return_value = None

    response = client.post("/sessions/non_existent_session/heartbeat")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_session_heartbeat_failure(mock_session_manager):
    """
    Test session heartbeat when the session manager fails.
    """
    mock_session_manager.get_session.side_effect = Exception("Session heartbeat failed")

    response = client.post("/sessions/any_session_id/heartbeat")

    assert response.status_code == 500
    assert "Failed to update heartbeat" in response.json()["detail"]


def test_get_session_stats_not_found(mock_session_manager):
    """
    Test getting stats for a session that does not exist.
    """
    mock_session_manager.get_session.return_value = None

    response = client.get("/sessions/non_existent_session/stats")

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_get_session_stats_failure(mock_session_manager):
    """
    Test getting session stats when the session manager fails.
    """
    mock_session_manager.get_session.side_effect = Exception("Session stats failed")

    response = client.get("/sessions/any_session_id/stats")

    assert response.status_code == 500
    assert "Failed to get session stats" in response.json()["detail"]
