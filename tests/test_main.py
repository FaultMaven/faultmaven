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
    
    data = response.json()
    
    # Check core health status
    assert data["status"] == "healthy"
    
    # Check required services exist
    assert "services" in data
    services = data["services"]
    assert services["session_manager"] == "active"
    assert services["api"] == "running"
    
    # Check architecture information is present
    assert "architecture" in data
    architecture = data["architecture"]
    assert "migration_strategy" in architecture
    assert "migration_safe" in architecture
    assert "using_refactored_api" in architecture
    assert "using_di_container" in architecture


def test_root_endpoint():
    """
    Tests the root (/) endpoint to ensure it returns the correct API
    information.
    """
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "FaultMaven API"
    assert "version" in response.json()
