"""Integration tests for User Management API endpoints (TASK-018)

Tests for:
- GET /api/v1/users/me - Get current user profile
- PATCH /api/v1/users/me - Update current user profile
- GET /api/v1/users/{user_id} - Get user by ID (admin only)
- GET /api/v1/users - List users (admin only)
- DELETE /api/v1/users/{user_id} - Deactivate user (admin only)
- POST /api/v1/users/{user_id}/activate - Activate user (admin only)

Coverage Target: 90%+
"""

from unittest.mock import patch, AsyncMock
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
def client(app: FastAPI) -> TestClient:
    """Create synchronous test client."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_auth_service():
    """Reset auth service before each test."""
    set_auth_service(None)
    yield
    set_auth_service(None)


def register_and_login(client: TestClient, email: str = None, password: str = "TestP@ssw0rd!"):
    """Helper to register and login a user, returning the access token."""
    if email is None:
        email = f"user_{uuid.uuid4().hex[:8]}@example.com"

    username = email.split('@')[0]  # Use email prefix as username

    # Register
    client.post(
        "/api/v1/auth/dev-register",
        json={
            "username": username,
            "password": password,
            "full_name": "Test User",
        },
    )

    # Login
    response = client.post(
        "/api/v1/auth/dev-login",
        json={"username": username, "password": password},
    )

    return response.json()["access_token"], email


def get_admin_token(client: TestClient):
    """Get an admin token using dev users."""
    response = client.post(
        "/api/v1/auth/dev-login",
        json={
            "username": "admin",
            "password": "password123",
        },
    )
    return response.json()["access_token"]


# ============================================================
# GET /users/me Tests
# ============================================================


class TestGetCurrentUserProfile:
    """Tests for GET /api/v1/users/me."""

    def test_get_me_returns_profile(self, client):
        """200 OK returns current user profile."""
        access_token, email = register_and_login(client)

        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["email"] == email
        assert "user_id" in data
        assert "full_name" in data
        assert "is_active" in data
        assert "is_verified" in data

    def test_get_me_requires_authentication(self, client):
        """401 Unauthorized without authentication."""
        response = client.get("/api/v1/users/me")
        assert response.status_code == 401

    def test_get_me_with_invalid_token(self, client):
        """401 Unauthorized with invalid token."""
        response = client.get(
            "/api/v1/users/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401


# ============================================================
# PATCH /users/me Tests
# ============================================================


class TestUpdateCurrentUserProfile:
    """Tests for PATCH /api/v1/users/me."""

    def test_update_full_name(self, client):
        """200 OK updates full name."""
        access_token, _ = register_and_login(client)

        response = client.patch(
            "/api/v1/users/me",
            json={"full_name": "New Name"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        assert response.json()["full_name"] == "New Name"

    def test_update_email(self, client):
        """200 OK updates email."""
        access_token, _ = register_and_login(client)
        new_email = f"new_{uuid.uuid4().hex[:8]}@example.com"

        response = client.patch(
            "/api/v1/users/me",
            json={"email": new_email},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        assert response.json()["email"] == new_email

    def test_update_email_sets_unverified(self, client):
        """Email change sets is_verified to false."""
        access_token, _ = register_and_login(client)
        new_email = f"verify_{uuid.uuid4().hex[:8]}@example.com"

        response = client.patch(
            "/api/v1/users/me",
            json={"email": new_email},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 200
        assert response.json()["is_verified"] is False

    def test_update_rejects_duplicate_email(self, client):
        """409 Conflict for duplicate email."""
        # Register two users
        access_token1, _ = register_and_login(client)
        _, email2 = register_and_login(client)

        # Try to change first user's email to second user's email
        response = client.patch(
            "/api/v1/users/me",
            json={"email": email2},
            headers={"Authorization": f"Bearer {access_token1}"},
        )

        assert response.status_code == 409

    def test_update_requires_authentication(self, client):
        """401 Unauthorized without authentication."""
        response = client.patch(
            "/api/v1/users/me",
            json={"full_name": "New Name"},
        )
        assert response.status_code == 401


# ============================================================
# GET /users Tests (Admin Only)
# ============================================================


class TestListUsers:
    """Tests for GET /api/v1/users."""

    def test_list_users_requires_admin(self, client):
        """403 Forbidden for non-admin users."""
        # Register regular user and get token
        access_token, _ = register_and_login(client)

        response = client.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 403

    def test_list_users_returns_users(self, client):
        """200 OK returns user list for admin."""
        admin_token = get_admin_token(client)

        response = client.get(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_list_users_respects_pagination(self, client):
        """List users respects limit and offset."""
        admin_token = get_admin_token(client)

        response = client.get(
            "/api/v1/users?limit=5&offset=0",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 5
        assert data["offset"] == 0


# ============================================================
# GET /users/{user_id} Tests (Admin Only)
# ============================================================


class TestGetUserById:
    """Tests for GET /api/v1/users/{user_id}."""

    def test_get_user_requires_admin(self, client):
        """403 Forbidden for non-admin users."""
        access_token, _ = register_and_login(client)

        response = client.get(
            "/api/v1/users/some-user-id",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 403

    def test_get_user_returns_user(self, client):
        """200 OK returns user for admin."""
        # Register a user to get a valid user_id
        register_response = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"getuser_{uuid.uuid4().hex[:8]}@example.com",
                "password": "TestP@ssw0rd!",
                "full_name": "Get User Test",
            },
        )
        user_id = register_response.json()["user_id"]

        admin_token = get_admin_token(client)

        response = client.get(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 200
        assert response.json()["user_id"] == user_id

    def test_get_user_returns_404_for_nonexistent(self, client):
        """404 Not Found for non-existent user."""
        admin_token = get_admin_token(client)

        response = client.get(
            "/api/v1/users/nonexistent-user-id",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 404


# ============================================================
# DELETE /users/{user_id} Tests (Admin Only)
# ============================================================


class TestDeactivateUser:
    """Tests for DELETE /api/v1/users/{user_id}."""

    def test_deactivate_requires_admin(self, client):
        """403 Forbidden for non-admin users."""
        access_token, _ = register_and_login(client)

        response = client.delete(
            "/api/v1/users/some-user-id",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 403

    def test_deactivate_user_success(self, client):
        """204 No Content on successful deactivation."""
        # Register a user to deactivate
        register_response = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"deact_{uuid.uuid4().hex[:8]}@example.com",
                "password": "TestP@ssw0rd!",
                "full_name": "Deactivate Test",
            },
        )
        user_id = register_response.json()["user_id"]

        admin_token = get_admin_token(client)

        response = client.delete(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 204

    def test_deactivated_user_cannot_login(self, client):
        """Deactivated user cannot login."""
        email = f"nologin_{uuid.uuid4().hex[:8]}@example.com"
        password = "TestP@ssw0rd!"

        # Register user
        register_response = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": password,
                "full_name": "No Login Test",
            },
        )
        user_id = register_response.json()["user_id"]

        # Deactivate
        admin_token = get_admin_token(client)
        client.delete(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        # Try to login
        login_response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )

        assert login_response.status_code == 401

    def test_cannot_deactivate_self(self, client):
        """403 Forbidden when trying to deactivate self."""
        admin_token = get_admin_token(client)

        # Get admin user_id from /me
        me_response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        admin_user_id = me_response.json()["user_id"]

        # Try to deactivate self
        response = client.delete(
            f"/api/v1/users/{admin_user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 403


# ============================================================
# POST /users/{user_id}/activate Tests (Admin Only)
# ============================================================


class TestActivateUser:
    """Tests for POST /api/v1/users/{user_id}/activate."""

    def test_activate_requires_admin(self, client):
        """403 Forbidden for non-admin users."""
        access_token, _ = register_and_login(client)

        response = client.post(
            "/api/v1/users/some-user-id/activate",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert response.status_code == 403

    def test_activate_user_success(self, client):
        """200 OK on successful activation."""
        # Register and deactivate a user
        email = f"activate_{uuid.uuid4().hex[:8]}@example.com"
        register_response = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "TestP@ssw0rd!",
                "full_name": "Activate Test",
            },
        )
        user_id = register_response.json()["user_id"]

        admin_token = get_admin_token(client)

        # Deactivate first
        client.delete(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        # Activate
        response = client.post(
            f"/api/v1/users/{user_id}/activate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        assert response.status_code == 200
        assert response.json()["is_active"] is True

    def test_activated_user_can_login(self, client):
        """Reactivated user can login again."""
        email = f"relogin_{uuid.uuid4().hex[:8]}@example.com"
        password = "TestP@ssw0rd!"

        # Register user
        register_response = client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": password,
                "full_name": "Re-login Test",
            },
        )
        user_id = register_response.json()["user_id"]

        admin_token = get_admin_token(client)

        # Deactivate
        client.delete(
            f"/api/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        # Activate
        client.post(
            f"/api/v1/users/{user_id}/activate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        # Try to login
        login_response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )

        assert login_response.status_code == 200
