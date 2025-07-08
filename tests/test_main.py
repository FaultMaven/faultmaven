"""tests/test_main.py

Purpose: Tests for the main FastAPI application lifecycle
"""

from fastapi.testclient import TestClient

from faultmaven.main import app

client = TestClient(app)


def test_health_check():
    """
    Tests the /health endpoint to ensure the application is running
    and responding correctly.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "services": {"session_manager": "active", "api": "running"},
    }


def test_root_endpoint():
    """
    Tests the root (/) endpoint to ensure it returns the correct API
    information.
    """
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "FaultMaven API"
    assert "version" in response.json()
