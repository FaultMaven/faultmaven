"""Verification test to understand why mocking fails in API integration tests.

This test investigates whether the auth mocking strategy works at all.

KNOWN ISSUE:
- test_with_mock_using_override_dependency currently fails
- The case routes use require_authentication which depends on get_current_user_optional
- Simple dependency override doesn't work - needs investigation into middleware interaction
- Working pattern exists in test_agent_api.py (overrides get_current_user from auth middleware)
- This test file should be refactored or removed once proper auth mocking pattern is documented
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


def test_with_mock_using_override_dependency(client, app):
    """Test using FastAPI dependency override (better approach)."""
    from datetime import datetime, timezone
    from faultmaven.api.v1.auth_dependencies import get_current_user_optional
    from faultmaven.modules.auth.domain.models.auth import DevUser

    # Create a proper DevUser (not AuthenticatedUser)
    mock_user = DevUser(
        user_id="test-user-123",
        username="testuser",
        email="test@example.com",
        display_name="Test User",
        created_at=datetime.now(timezone.utc),
        is_dev_user=True,
        is_active=True,
        roles=["admin"]
    )

    async def override_get_current_user_optional():
        return mock_user

    # Override the correct dependency (get_current_user_optional, not get_current_user)
    app.dependency_overrides[get_current_user_optional] = override_get_current_user_optional

    try:
        response = client.get("/api/v1/cases")
        print(f"Status code: {response.status_code}")
        print(f"Response: {response.text[:200]}")

        # With dependency override, should work
        assert response.status_code != 401, "Dependency override failed"

    finally:
        # Clean up
        app.dependency_overrides.clear()
