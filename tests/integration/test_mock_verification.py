"""Verification test to understand why mocking fails in API integration tests.

This test investigates whether the auth mocking strategy works at all.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.models.auth import AuthenticatedUser


@pytest.fixture
def app():
    """Create FastAPI test application."""
    return main_app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_user():
    """Create a mock authenticated user."""
    return AuthenticatedUser(
        user_id="test-user-123",
        organization_id="org-456",
        email="test@example.com",
        roles=["admin"],
        permissions=["cases:read", "cases:write"],
        token_jti="token-789",
    )


def test_mock_interception_patch_get_auth_service(client, mock_user):
    """Test if patching get_auth_service works."""
    with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
        # Setup mock
        mock_auth = MagicMock()
        mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(return_value=mock_user)
        mock_get_auth.return_value = mock_auth

        # Make request
        response = client.get(
            "/api/v1/cases",
            headers={"Authorization": "Bearer test-token"},
        )

        # Verify - if mocking works, we should NOT get 401
        print(f"Status code: {response.status_code}")
        print(f"Response: {response.text}")
        print(f"Mock called: {mock_get_auth.called}")
        print(f"Extract user called: {mock_auth.extract_user_from_token_with_revocation_check.called}")

        # This will tell us if mocking is working
        assert response.status_code != 401, "Mocking failed - still getting 401"


def test_no_auth_returns_401(client):
    """Baseline test - without auth header, should get 401."""
    response = client.get("/api/v1/cases")
    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_with_mock_using_override_dependency(client, app, mock_user):
    """Test using FastAPI dependency override (better approach)."""
    from faultmaven.api.middleware.auth import get_current_user

    async def override_get_current_user():
        return mock_user

    # Override the dependency
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        response = client.get("/api/v1/cases")
        print(f"Status code: {response.status_code}")
        print(f"Response: {response.text[:200]}")

        # With dependency override, should work
        assert response.status_code != 401, "Dependency override failed"

    finally:
        # Clean up
        app.dependency_overrides.clear()
