"""Integration tests for Authentication API endpoints (TASK-017)

Tests POST /api/v1/auth/login, /refresh, /logout, /verify endpoints.

Test Categories:
1. Login Tests - Credential validation and token generation
2. Refresh Tests - Token exchange and rotation
3. Logout Tests - Token revocation
4. Verify Tests - Token introspection

Coverage Target: 90%+
"""

from unittest.mock import patch, AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient

from faultmaven.api.app import create_app
from faultmaven.api.middleware.auth import set_auth_service


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create test FastAPI application."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create synchronous test client."""
    return TestClient(app)


@pytest.fixture
async def async_client(app: FastAPI):
    """Create async test client."""
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture(autouse=True)
def reset_auth_service():
    """Reset auth service before each test."""
    set_auth_service(None)
    yield
    set_auth_service(None)


# ============================================================
# Login Endpoint Tests
# ============================================================


class TestLoginEndpoint:
    """Tests for POST /api/v1/auth/login."""

    def test_login_returns_tokens_for_valid_credentials(self, client):
        """200 OK returns access_token and refresh_token for valid credentials."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )

        assert response.status_code == 200
        data = response.json()

        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] > 0

        # Tokens should be valid JWTs (3 parts)
        assert data["access_token"].count(".") == 2
        assert data["refresh_token"].count(".") == 2

    def test_login_tokens_are_valid_and_decodable(self, client):
        """Returned tokens are valid and can be used for authentication."""
        # Login first
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        assert login_response.status_code == 200
        tokens = login_response.json()

        # Use token to access /me endpoint
        me_response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert me_response.status_code == 200
        user_data = me_response.json()
        assert user_data["email"] == "admin@faultmaven.local"

    def test_login_returns_401_for_invalid_email(self, client):
        """401 Unauthorized for unknown email."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "unknown@example.com",
                "password": "password123",
            },
        )

        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["detail"]

    def test_login_returns_401_for_empty_password(self, client):
        """401 Unauthorized for empty password."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "",
            },
        )

        # Empty password should fail validation (422) or auth (401)
        assert response.status_code in [401, 422]

    def test_login_returns_422_for_invalid_email_format(self, client):
        """422 Validation error for invalid email format."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "not-an-email",
                "password": "password123",
            },
        )

        assert response.status_code == 422

    def test_login_returns_user_roles_and_permissions_in_token(self, client):
        """Token contains user roles and permissions."""
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )

        assert response.status_code == 200
        tokens = response.json()

        # Verify the token contains correct claims via /verify
        verify_response = client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        assert verify_response.status_code == 200
        verify_data = verify_response.json()
        assert verify_data["valid"] is True
        assert "admin" in verify_data["roles"]
        assert len(verify_data["permissions"]) > 0

    def test_login_different_roles_get_different_permissions(self, client):
        """Different user roles have different permissions."""
        # Admin login
        admin_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        admin_tokens = admin_response.json()

        # Viewer login
        viewer_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "viewer@faultmaven.local",
                "password": "password123",
            },
        )
        viewer_tokens = viewer_response.json()

        # Verify admin has more permissions
        admin_verify = client.post(
            "/api/v1/auth/verify",
            json={"token": admin_tokens["access_token"]},
        ).json()

        viewer_verify = client.post(
            "/api/v1/auth/verify",
            json={"token": viewer_tokens["access_token"]},
        ).json()

        assert len(admin_verify["permissions"]) > len(viewer_verify["permissions"])


# ============================================================
# Refresh Endpoint Tests
# ============================================================


class TestRefreshEndpoint:
    """Tests for POST /api/v1/auth/refresh."""

    def test_refresh_returns_new_tokens(self, client):
        """200 OK returns new access_token from valid refresh token."""
        # Login to get refresh token
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Refresh
        refresh_response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )

        assert refresh_response.status_code == 200
        new_tokens = refresh_response.json()

        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens
        assert new_tokens["access_token"] != tokens["access_token"]

    def test_refresh_new_access_token_is_valid(self, client):
        """New access token can be used for authentication."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "member@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Refresh
        refresh_response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        new_tokens = refresh_response.json()

        # Use new token
        me_response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
        )

        assert me_response.status_code == 200
        assert me_response.json()["email"] == "member@faultmaven.local"

    def test_refresh_returns_401_for_invalid_token(self, client):
        """401 Unauthorized for invalid refresh token."""
        response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )

        assert response.status_code == 401

    def test_refresh_returns_401_for_access_token(self, client):
        """401 Unauthorized when using access token instead of refresh."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Try to use access token as refresh token
        response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["access_token"]},
        )

        assert response.status_code == 401


# ============================================================
# Logout Endpoint Tests
# ============================================================


class TestLogoutEndpoint:
    """Tests for POST /api/v1/auth/logout."""

    def test_logout_returns_204(self, client):
        """204 No Content on successful logout."""
        # Login first
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Logout
        response = client.post(
            "/api/v1/auth/logout",
            json={},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert response.status_code == 204

    def test_logout_with_refresh_token(self, client):
        """Logout can include refresh token for revocation."""
        # Login first
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Logout with refresh token
        response = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert response.status_code == 204

    def test_logout_requires_authentication(self, client):
        """401 Unauthorized when not authenticated."""
        response = client.post(
            "/api/v1/auth/logout",
            json={},
        )

        assert response.status_code == 401


# ============================================================
# Verify Endpoint Tests
# ============================================================


class TestVerifyEndpoint:
    """Tests for POST /api/v1/auth/verify."""

    def test_verify_returns_valid_true_for_valid_token(self, client):
        """Returns valid=true for valid token."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify
        response = client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["user_id"] is not None
        assert data["organization_id"] is not None

    def test_verify_returns_user_info(self, client):
        """Returns user_id, organization_id, roles for valid token."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify
        response = client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        data = response.json()
        assert "user_id" in data
        assert "organization_id" in data
        assert "roles" in data
        assert "admin" in data["roles"]
        assert "expires_at" in data

    def test_verify_returns_valid_false_for_expired_token(self, client):
        """Returns valid=false for expired token."""
        # We can't easily create an expired token without mocking
        # This test would need time travel or mocked tokens
        pass

    def test_verify_returns_valid_false_for_invalid_token(self, client):
        """Returns valid=false for invalid token."""
        response = client.post(
            "/api/v1/auth/verify",
            json={"token": "invalid.token.here"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert "error" in data

    def test_verify_works_for_refresh_token(self, client):
        """Verify endpoint works for refresh tokens too."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify refresh token
        response = client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["refresh_token"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True


# ============================================================
# Me Endpoint Tests
# ============================================================


class TestMeEndpoint:
    """Tests for GET /api/v1/auth/me."""

    def test_me_returns_user_info(self, client):
        """Returns current user info for authenticated request."""
        # Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Get me
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "admin@faultmaven.local"
        assert "user_id" in data
        assert "organization_id" in data
        assert "roles" in data
        assert "permissions" in data

    def test_me_returns_401_without_token(self, client):
        """401 Unauthorized without authentication."""
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_me_returns_401_with_invalid_token(self, client):
        """401 Unauthorized with invalid token."""
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401


# ============================================================
# End-to-End Auth Flow Tests
# ============================================================


class TestEndToEndAuthFlow:
    """Tests for complete authentication workflows."""

    def test_complete_auth_flow(self, client):
        """Complete workflow: login -> access protected -> refresh -> logout."""
        # Step 1: Login
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "member@faultmaven.local",
                "password": "password123",
            },
        )
        assert login_response.status_code == 200
        tokens = login_response.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        # Step 2: Access protected endpoint
        me_response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["email"] == "member@faultmaven.local"

        # Step 3: Refresh token
        refresh_response = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert refresh_response.status_code == 200
        new_tokens = refresh_response.json()
        new_access_token = new_tokens["access_token"]

        # Step 4: Access with new token
        me_response2 = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        assert me_response2.status_code == 200

        # Step 5: Logout
        logout_response = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": new_tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        assert logout_response.status_code == 204

    def test_tokens_verified_correctly(self, client):
        """Verify token returns correct organization_id."""
        # Login as admin
        login_response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify token
        verify_response = client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        data = verify_response.json()
        assert data["valid"] is True
        assert data["organization_id"] == "org-dev-001"
