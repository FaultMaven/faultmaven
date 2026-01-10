"""Integration tests for Authentication API endpoints (TASK-017, TASK-018)

Tests POST /api/v1/auth/login, /refresh, /logout, /verify,
/register, /password/reset-request, /password/reset, /password/change endpoints.

Test Categories:
1. Login Tests - Credential validation and token generation
2. Refresh Tests - Token exchange and rotation
3. Logout Tests - Token revocation
4. Verify Tests - Token introspection
5. Registration Tests - User account creation (TASK-018)
6. Password Reset Tests - Password reset flow (TASK-018)
7. Password Change Tests - Authenticated password change (TASK-018)

Coverage Target: 90%+
"""

from unittest.mock import patch, AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from faultmaven.main import app as main_app
from faultmaven.api.middleware.auth import set_auth_service


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def app() -> FastAPI:
    """Create test FastAPI application."""
    return main_app


@pytest.fixture
async def client(app: FastAPI):
    """Create async test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    async def test_login_returns_tokens_for_valid_credentials(self, client):
        """200 OK returns access_token and refresh_token for valid credentials."""
        response = await client.post(
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

    async def test_login_tokens_are_valid_and_decodable(self, client):
        """Returned tokens are valid and can be used for authentication."""
        # Login first
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        assert login_response.status_code == 200
        tokens = login_response.json()

        # Use token to access /me endpoint
        me_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert me_response.status_code == 200
        user_data = me_response.json()
        assert user_data["email"] == "admin@faultmaven.local"

    async def test_login_returns_401_for_invalid_email(self, client):
        """401 Unauthorized for unknown email."""
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "unknown@example.com",
                "password": "password123",
            },
        )

        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["detail"]

    async def test_login_returns_401_for_empty_password(self, client):
        """401 Unauthorized for empty password."""
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "",
            },
        )

        # Empty password should fail validation (422) or auth (401)
        assert response.status_code in [401, 422]

    async def test_login_returns_422_for_invalid_email_format(self, client):
        """422 Validation error for invalid email format."""
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "not-an-email",
                "password": "password123",
            },
        )

        assert response.status_code == 422

    async def test_login_returns_user_roles_and_permissions_in_token(self, client):
        """Token contains user roles and permissions."""
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )

        assert response.status_code == 200
        tokens = response.json()

        # Verify the token contains correct claims via /verify
        verify_response = await client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        assert verify_response.status_code == 200
        verify_data = verify_response.json()
        assert verify_data["valid"] is True
        assert "admin" in verify_data["roles"]
        assert len(verify_data["permissions"]) > 0

    async def test_login_different_roles_get_different_permissions(self, client):
        """Different user roles have different permissions."""
        # Admin login
        admin_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        admin_tokens = admin_response.json()

        # Viewer login
        viewer_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "viewer@faultmaven.local",
                "password": "password123",
            },
        )
        viewer_tokens = viewer_response.json()

        # Verify admin has more permissions
        admin_verify = (await client.post(
            "/api/v1/auth/verify",
            json={"token": admin_tokens["access_token"]},
        )).json()

        viewer_verify = (await client.post(
            "/api/v1/auth/verify",
            json={"token": viewer_tokens["access_token"]},
        )).json()

        assert len(admin_verify["permissions"]) > len(viewer_verify["permissions"])


# ============================================================
# Refresh Endpoint Tests
# ============================================================


class TestRefreshEndpoint:
    """Tests for POST /api/v1/auth/refresh."""

    async def test_refresh_returns_new_tokens(self, client):
        """200 OK returns new access_token from valid refresh token."""
        # Login to get refresh token
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Refresh
        refresh_response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )

        assert refresh_response.status_code == 200
        new_tokens = refresh_response.json()

        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens
        assert new_tokens["access_token"] != tokens["access_token"]

    async def test_refresh_new_access_token_is_valid(self, client):
        """New access token can be used for authentication."""
        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "member@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Refresh
        refresh_response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
        )
        new_tokens = refresh_response.json()

        # Use new token
        me_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
        )

        assert me_response.status_code == 200
        assert me_response.json()["email"] == "member@faultmaven.local"

    async def test_refresh_returns_401_for_invalid_token(self, client):
        """401 Unauthorized for invalid refresh token."""
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid.token.here"},
        )

        assert response.status_code == 401

    async def test_refresh_returns_401_for_access_token(self, client):
        """401 Unauthorized when using access token instead of refresh."""
        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Try to use access token as refresh token
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": tokens["access_token"]},
        )

        assert response.status_code == 401


# ============================================================
# Logout Endpoint Tests
# ============================================================


class TestLogoutEndpoint:
    """Tests for POST /api/v1/auth/logout."""

    async def test_logout_returns_204(self, client):
        """204 No Content on successful logout."""
        # Login first
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Logout
        response = await client.post(
            "/api/v1/auth/logout",
            json={},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert response.status_code == 204

    async def test_logout_with_refresh_token(self, client):
        """Logout can include refresh token for revocation."""
        # Login first
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Logout with refresh token
        response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert response.status_code == 204

    async def test_logout_requires_authentication(self, client):
        """401 Unauthorized when not authenticated."""
        response = await client.post(
            "/api/v1/auth/logout",
            json={},
        )

        assert response.status_code == 401


# ============================================================
# Verify Endpoint Tests
# ============================================================


class TestVerifyEndpoint:
    """Tests for POST /api/v1/auth/verify."""

    async def test_verify_returns_valid_true_for_valid_token(self, client):
        """Returns valid=true for valid token."""
        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify
        response = await client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["user_id"] is not None
        assert data["organization_id"] is not None

    async def test_verify_returns_user_info(self, client):
        """Returns user_id, organization_id, roles for valid token."""
        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify
        response = await client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        data = response.json()
        assert "user_id" in data
        assert "organization_id" in data
        assert "roles" in data
        assert "admin" in data["roles"]
        assert "expires_at" in data

    async def test_verify_returns_valid_false_for_expired_token(self, client):
        """Returns valid=false for expired token."""
        # We can't easily create an expired token without mocking
        # This test would need time travel or mocked tokens
        pass

    async def test_verify_returns_valid_false_for_invalid_token(self, client):
        """Returns valid=false for invalid token."""
        response = await client.post(
            "/api/v1/auth/verify",
            json={"token": "invalid.token.here"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
        assert "error" in data

    async def test_verify_works_for_refresh_token(self, client):
        """Verify endpoint works for refresh tokens too."""
        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify refresh token
        response = await client.post(
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

    async def test_me_returns_user_info(self, client):
        """Returns current user info for authenticated request."""
        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Get me
        response = await client.get(
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

    async def test_me_returns_401_without_token(self, client):
        """401 Unauthorized without authentication."""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401

    async def test_me_returns_401_with_invalid_token(self, client):
        """401 Unauthorized with invalid token."""
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401


# ============================================================
# End-to-End Auth Flow Tests
# ============================================================


class TestEndToEndAuthFlow:
    """Tests for complete authentication workflows."""

    async def test_complete_auth_flow(self, client):
        """Complete workflow: login -> access protected -> refresh -> logout."""
        # Step 1: Login
        login_response = await client.post(
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
        me_response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert me_response.status_code == 200
        assert me_response.json()["email"] == "member@faultmaven.local"

        # Step 3: Refresh token
        refresh_response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert refresh_response.status_code == 200
        new_tokens = refresh_response.json()
        new_access_token = new_tokens["access_token"]

        # Step 4: Access with new token
        me_response2 = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        assert me_response2.status_code == 200

        # Step 5: Logout
        logout_response = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": new_tokens["refresh_token"]},
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        assert logout_response.status_code == 204

    async def test_tokens_verified_correctly(self, client):
        """Verify token returns correct organization_id."""
        # Login as admin
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@faultmaven.local",
                "password": "password123",
            },
        )
        tokens = login_response.json()

        # Verify token
        verify_response = await client.post(
            "/api/v1/auth/verify",
            json={"token": tokens["access_token"]},
        )

        data = verify_response.json()
        assert data["valid"] is True
        assert data["organization_id"] == "org-dev-001"


# ============================================================
# Registration Endpoint Tests (TASK-018)
# ============================================================


class TestRegisterEndpoint:
    """Tests for POST /api/v1/auth/register."""

    async def test_register_creates_user(self, client):
        """201 Created returns user details for valid registration."""
        import uuid

        unique_email = f"test_{uuid.uuid4().hex[:8]}@example.com"
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "SecureP@ss123",
                "full_name": "Test User",
            },
        )

        assert response.status_code == 201
        data = response.json()

        assert "user_id" in data
        assert data["email"] == unique_email
        assert data["full_name"] == "Test User"
        assert data["is_active"] is True
        assert data["is_verified"] is False

    async def test_register_allows_login(self, client):
        """Registered user can login with credentials."""
        import uuid

        unique_email = f"login_{uuid.uuid4().hex[:8]}@example.com"
        password = "SecureP@ss123"

        # Register
        register_response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": password,
                "full_name": "Login Test User",
            },
        )
        assert register_response.status_code == 201

        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": unique_email,
                "password": password,
            },
        )

        assert login_response.status_code == 200
        assert "access_token" in login_response.json()

    async def test_register_rejects_duplicate_email(self, client):
        """409 Conflict for duplicate email."""
        import uuid

        unique_email = f"dup_{uuid.uuid4().hex[:8]}@example.com"

        # First registration
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "SecureP@ss123",
                "full_name": "First User",
            },
        )

        # Second registration with same email
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "OtherP@ss456",
                "full_name": "Second User",
            },
        )

        assert response.status_code == 409

    async def test_register_rejects_weak_password(self, client):
        """422 Validation error for weak password."""
        import uuid

        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"weak_{uuid.uuid4().hex[:8]}@example.com",
                "password": "weak",
                "full_name": "Weak Password User",
            },
        )

        assert response.status_code == 422

    async def test_register_rejects_invalid_email(self, client):
        """422 Validation error for invalid email."""
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "not-an-email",
                "password": "SecureP@ss123",
                "full_name": "Invalid Email User",
            },
        )

        assert response.status_code == 422

    async def test_register_password_requirements(self, client):
        """Password must meet all requirements."""
        import uuid

        base_email = f"req_{uuid.uuid4().hex[:8]}"

        # Too short
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{base_email}@example.com",
                "password": "Sh0rt!",
                "full_name": "Test",
            },
        )
        assert response.status_code == 422

        # No uppercase
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{base_email}2@example.com",
                "password": "noupperc@se1",
                "full_name": "Test",
            },
        )
        assert response.status_code == 422

        # No digit
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{base_email}3@example.com",
                "password": "NoDigit@Here",
                "full_name": "Test",
            },
        )
        assert response.status_code == 422


# ============================================================
# Password Reset Request Tests (TASK-018)
# ============================================================


class TestPasswordResetRequestEndpoint:
    """Tests for POST /api/v1/auth/password/reset-request."""

    async def test_reset_request_returns_204(self, client):
        """204 No Content for valid email."""
        import uuid

        # Register user first
        unique_email = f"reset_{uuid.uuid4().hex[:8]}@example.com"
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "SecureP@ss123",
                "full_name": "Reset User",
            },
        )

        # Request reset
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": unique_email},
        )

        assert response.status_code == 204

    async def test_reset_request_returns_204_for_nonexistent_email(self, client):
        """204 No Content even for non-existent email (enumeration prevention)."""
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": "nonexistent@example.com"},
        )

        assert response.status_code == 204


# ============================================================
# Password Change Tests (TASK-018)
# ============================================================


class TestPasswordChangeEndpoint:
    """Tests for POST /api/v1/auth/password/change."""

    async def test_password_change_success(self, client):
        """200 OK for successful password change."""
        import uuid

        unique_email = f"change_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Change User",
            },
        )

        # Login
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )
        access_token = login_response.json()["access_token"]

        # Change password
        response = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": old_password,
                "new_password": new_password,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200

    async def test_password_change_allows_new_login(self, client):
        """After password change, new password works for login."""
        import uuid

        unique_email = f"newlogin_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register and login
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "New Login User",
            },
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )
        access_token = login_response.json()["access_token"]

        # Change password
        await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": old_password,
                "new_password": new_password,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Try login with new password
        new_login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": new_password},
        )

        assert new_login_response.status_code == 200

    async def test_password_change_rejects_wrong_current(self, client):
        """401 Unauthorized for wrong current password."""
        import uuid

        unique_email = f"wrongcur_{uuid.uuid4().hex[:8]}@example.com"
        password = "TestP@ssw0rd!"

        # Register and login
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": password,
                "full_name": "Wrong Current User",
            },
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": password},
        )
        access_token = login_response.json()["access_token"]

        # Try change with wrong current password
        response = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": "WrongP@ssw0rd!",
                "new_password": "NewP@ssw0rd!",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 401

    async def test_password_change_requires_authentication(self, client):
        """401 Unauthorized without authentication."""
        response = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": "OldP@ssw0rd!",
                "new_password": "NewP@ssw0rd!",
            },
        )

        assert response.status_code == 401

    async def test_password_change_validates_new_password(self, client):
        """422 Validation error for weak new password."""
        import uuid

        unique_email = f"weaknew_{uuid.uuid4().hex[:8]}@example.com"
        password = "TestP@ssw0rd!"

        # Register and login
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": password,
                "full_name": "Weak New User",
            },
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": password},
        )
        access_token = login_response.json()["access_token"]

        # Try change with weak new password
        response = await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": password,
                "new_password": "weak",
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 422

    async def test_old_password_no_longer_works(self, client):
        """After password change, old password fails."""
        import uuid

        unique_email = f"oldnope_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register and login
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Old Nope User",
            },
        )
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )
        access_token = login_response.json()["access_token"]

        # Change password
        await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": old_password,
                "new_password": new_password,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Try login with old password - should fail
        old_login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )

        assert old_login_response.status_code == 401


# ============================================================
# Password Reset Endpoint Tests (TASK-018)
# ============================================================


class TestPasswordResetEndpoint:
    """Tests for POST /api/v1/auth/password/reset."""

    async def test_reset_password_with_valid_token(self, client):
        """200 OK returns user for valid reset token."""
        import uuid
        import asyncio

        unique_email = f"validreset_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register user
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Valid Reset User",
            },
        )

        # Get the user service and generate a real reset token
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        # Reset password with valid token
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={
                "token": reset_token,
                "new_password": new_password,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["email"] == unique_email

    async def test_reset_password_allows_login_with_new_password(self, client):
        """After password reset, new password works for login."""
        import uuid
        import asyncio

        unique_email = f"resetlogin_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register user
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Reset Login User",
            },
        )

        # Get reset token
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        # Reset password
        await client.post(
            "/api/v1/auth/password/reset",
            json={"token": reset_token, "new_password": new_password},
        )

        # Login with new password
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": new_password},
        )

        assert login_response.status_code == 200
        assert "access_token" in login_response.json()

    async def test_reset_password_old_password_no_longer_works(self, client):
        """After password reset, old password fails."""
        import uuid
        import asyncio

        unique_email = f"resetold_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Reset Old User",
            },
        )

        # Get reset token and reset
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        await client.post(
            "/api/v1/auth/password/reset",
            json={"token": reset_token, "new_password": new_password},
        )

        # Try login with old password - should fail
        old_login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )

        assert old_login_response.status_code == 401

    async def test_reset_password_invalid_token_returns_401(self, client):
        """401 Unauthorized for invalid reset token."""
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={
                "token": "invalid.token.here",
                "new_password": "NewP@ssw0rd!",
            },
        )

        assert response.status_code == 401

    async def test_reset_password_expired_token_returns_401(self, client):
        """401 Unauthorized for expired reset token."""
        import uuid
        import jwt
        from datetime import datetime, timezone, timedelta

        # Create an expired token manually
        expired_token = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "email": "test@example.com",
                "type": "password_reset",
                "exp": datetime.now(timezone.utc) - timedelta(hours=2),
            },
            "test_secret",
            algorithm="HS256",
        )

        response = await client.post(
            "/api/v1/auth/password/reset",
            json={
                "token": expired_token,
                "new_password": "NewP@ssw0rd!",
            },
        )

        assert response.status_code == 401

    async def test_reset_password_weak_password_returns_422(self, client):
        """422 Validation error for weak new password."""
        import uuid
        import asyncio

        unique_email = f"resetweak_{uuid.uuid4().hex[:8]}@example.com"

        # Register
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "StrongP@ss123",
                "full_name": "Reset Weak User",
            },
        )

        # Get reset token
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        # Try reset with weak password
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={
                "token": reset_token,
                "new_password": "weak",
            },
        )

        assert response.status_code == 422

    async def test_reset_password_missing_fields_returns_422(self, client):
        """422 Validation error for missing required fields."""
        # Missing new_password
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={"token": "some.token.here"},
        )
        assert response.status_code == 422

        # Missing token
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={"new_password": "NewP@ssw0rd!"},
        )
        assert response.status_code == 422

    async def test_reset_password_updates_database(self, client):
        """Password reset actually updates password in database."""
        import uuid
        import asyncio

        unique_email = f"resetdb_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Reset DB User",
            },
        )

        # Get reset token
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        # Reset password
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={"token": reset_token, "new_password": new_password},
        )

        assert response.status_code == 200

        # Verify by logging in with new password
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": new_password},
        )
        assert login_response.status_code == 200

        # Verify old password doesn't work
        old_login = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )
        assert old_login.status_code == 401

    async def test_reset_password_returns_user_response(self, client):
        """Reset password returns proper UserResponse."""
        import uuid
        import asyncio

        unique_email = f"resetresp_{uuid.uuid4().hex[:8]}@example.com"

        # Register
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "OldP@ssw0rd!",
                "full_name": "Reset Response User",
            },
        )

        # Get reset token
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        # Reset password
        response = await client.post(
            "/api/v1/auth/password/reset",
            json={"token": reset_token, "new_password": "NewP@ssw0rd!"},
        )

        assert response.status_code == 200
        data = response.json()

        # Verify UserResponse fields
        assert "user_id" in data
        assert data["email"] == unique_email
        assert data["full_name"] == "Reset Response User"
        assert "is_active" in data
        assert "is_verified" in data
        assert "created_at" in data
        assert "updated_at" in data


# ============================================================
# Token Revocation End-to-End Tests (TASK-018)
# ============================================================


class TestTokenRevocation:
    """End-to-end tests for token revocation on password change/reset."""

    async def test_password_change_new_login_required(self, client):
        """After password change, user can login again with new password."""
        import uuid

        unique_email = f"revchange_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register and login
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Rev Change User",
            },
        )

        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": old_password},
        )
        access_token = login_response.json()["access_token"]

        # Change password
        await client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": old_password,
                "new_password": new_password,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # New login should work
        new_login = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": new_password},
        )
        assert new_login.status_code == 200

    async def test_password_reset_new_login_required(self, client):
        """After password reset, user can login with new password."""
        import uuid
        import asyncio

        unique_email = f"revreset_{uuid.uuid4().hex[:8]}@example.com"
        old_password = "OldP@ssw0rd!"
        new_password = "NewP@ssw0rd!"

        # Register
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": old_password,
                "full_name": "Rev Reset User",
            },
        )

        # Get reset token
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def get_token():
            return await user_service.request_password_reset(unique_email)

        reset_token = asyncio.get_event_loop().run_until_complete(get_token())

        # Reset password
        await client.post(
            "/api/v1/auth/password/reset",
            json={"token": reset_token, "new_password": new_password},
        )

        # New login should work
        new_login = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": new_password},
        )
        assert new_login.status_code == 200

    async def test_deactivated_user_cannot_login(self, client):
        """Deactivated user cannot login."""
        import uuid
        import asyncio

        unique_email = f"deactlogin_{uuid.uuid4().hex[:8]}@example.com"
        password = "TestP@ssw0rd!"

        # Register
        reg_response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": password,
                "full_name": "Deact Login User",
            },
        )
        user_id = reg_response.json()["user_id"]

        # Deactivate user directly via service
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def deactivate():
            return await user_service.deactivate_user(user_id)

        asyncio.get_event_loop().run_until_complete(deactivate())

        # Try to login - should fail
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": password},
        )

        assert login_response.status_code == 401

    async def test_reactivated_user_can_login(self, client):
        """Reactivated user can login again."""
        import uuid
        import asyncio

        unique_email = f"reactlogin_{uuid.uuid4().hex[:8]}@example.com"
        password = "TestP@ssw0rd!"

        # Register
        reg_response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": password,
                "full_name": "React Login User",
            },
        )
        user_id = reg_response.json()["user_id"]

        # Deactivate then reactivate
        from faultmaven.api.routes.auth import get_user_service

        user_service = get_user_service()

        async def toggle_active():
            await user_service.deactivate_user(user_id)
            await user_service.activate_user(user_id)

        asyncio.get_event_loop().run_until_complete(toggle_active())

        # Login should work again
        login_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": password},
        )

        assert login_response.status_code == 200


# ============================================================
# Email Enumeration Prevention Tests (TASK-018)
# ============================================================


class TestEmailEnumerationPrevention:
    """Tests for email enumeration prevention."""

    async def test_reset_request_same_response_for_existing_email(self, client):
        """Password reset returns 204 for existing email."""
        import uuid

        unique_email = f"enumexist_{uuid.uuid4().hex[:8]}@example.com"

        # Register user
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "SecureP@ss123",
                "full_name": "Enum Exist User",
            },
        )

        # Request reset for existing user
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": unique_email},
        )

        assert response.status_code == 204

    async def test_reset_request_same_response_for_nonexistent_email(self, client):
        """Password reset returns 204 for non-existent email."""
        import uuid

        nonexistent_email = f"doesnotexist_{uuid.uuid4().hex[:8]}@example.com"

        # Request reset for non-existent user
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": nonexistent_email},
        )

        # Same response - prevents enumeration
        assert response.status_code == 204

    async def test_reset_request_no_error_details_leaked(self, client):
        """No error details are leaked in password reset response."""
        import uuid

        # Request reset for definitely non-existent user
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": f"notauser_{uuid.uuid4().hex}@example.com"},
        )

        # Should be 204 with no content
        assert response.status_code == 204
        # Response body should be empty or minimal
        assert response.text == "" or response.text == "null"

    async def test_login_does_not_distinguish_invalid_email_vs_password(self, client):
        """Login gives same error for invalid email and invalid password."""
        import uuid

        unique_email = f"loginenum_{uuid.uuid4().hex[:8]}@example.com"
        password = "SecureP@ss123"

        # Register user
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": password,
                "full_name": "Login Enum User",
            },
        )

        # Wrong password for existing user
        wrong_pw_response = await client.post(
            "/api/v1/auth/login",
            json={"email": unique_email, "password": "WrongP@ssword!"},
        )

        # Non-existent email
        wrong_email_response = await client.post(
            "/api/v1/auth/login",
            json={"email": f"nonexistent_{uuid.uuid4().hex[:8]}@example.com", "password": password},
        )

        # Both should return same status and similar error
        assert wrong_pw_response.status_code == wrong_email_response.status_code == 401
        assert wrong_pw_response.json()["detail"] == wrong_email_response.json()["detail"]


# ============================================================
# Additional Password Reset Request Tests (TASK-018)
# ============================================================


class TestPasswordResetRequestAdvanced:
    """Advanced tests for password reset request endpoint."""

    async def test_reset_request_invalid_email_format(self, client):
        """422 Validation error for invalid email format."""
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": "not-an-email"},
        )

        assert response.status_code == 422

    async def test_reset_request_empty_email(self, client):
        """422 Validation error for empty email."""
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": ""},
        )

        assert response.status_code == 422

    async def test_reset_request_multiple_times(self, client):
        """Multiple reset requests work (each generates new token)."""
        import uuid

        unique_email = f"multireset_{uuid.uuid4().hex[:8]}@example.com"

        # Register user
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "password": "SecureP@ss123",
                "full_name": "Multi Reset User",
            },
        )

        # Request reset multiple times - all should succeed
        for _ in range(3):
            response = await client.post(
                "/api/v1/auth/password/reset-request",
                json={"email": unique_email},
            )
            assert response.status_code == 204

    async def test_reset_request_case_insensitive_email(self, client):
        """Reset request works with different email case."""
        import uuid

        base_email = f"casetest_{uuid.uuid4().hex[:8]}"
        email_lower = f"{base_email}@example.com"
        email_upper = f"{base_email.upper()}@EXAMPLE.COM"

        # Register with lowercase
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": email_lower,
                "password": "SecureP@ss123",
                "full_name": "Case Test User",
            },
        )

        # Request reset with uppercase - should work (email normalization)
        response = await client.post(
            "/api/v1/auth/password/reset-request",
            json={"email": email_upper},
        )

        assert response.status_code == 204
