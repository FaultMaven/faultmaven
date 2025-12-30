"""Integration tests for Admin API Endpoints (TASK-019)

Tests admin-only user management endpoints:
1. GET /api/v1/admin/users - List users
2. GET /api/v1/admin/users/{user_id} - Get user details
3. POST /api/v1/admin/users/{user_id}/deactivate - Deactivate user
4. POST /api/v1/admin/users/{user_id}/activate - Activate user
5. POST /api/v1/admin/users/{user_id}/roles - Assign role
6. DELETE /api/v1/admin/users/{user_id}/roles/{role} - Remove role

Coverage Target: 90%+
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.api.app import create_app
from faultmaven.models.auth import AuthenticatedUser, DevUser
from faultmaven.models.rbac import get_permissions_for_roles
from faultmaven.services.auth_service import AuthenticationError


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def app():
    """Create FastAPI test application."""
    return create_app()


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_user():
    """Create admin authenticated user."""
    permissions = [p.value for p in get_permissions_for_roles(["admin"])]
    return AuthenticatedUser(
        user_id="admin-1",
        organization_id="org-123",
        email="admin@example.com",
        roles=["admin"],
        permissions=permissions,
        token_jti="token-admin",
    )


@pytest.fixture
def member_user():
    """Create member authenticated user."""
    permissions = [p.value for p in get_permissions_for_roles(["member"])]
    return AuthenticatedUser(
        user_id="member-1",
        organization_id="org-123",
        email="member@example.com",
        roles=["member"],
        permissions=permissions,
        token_jti="token-member",
    )


@pytest.fixture
def viewer_user():
    """Create viewer authenticated user."""
    permissions = [p.value for p in get_permissions_for_roles(["viewer"])]
    return AuthenticatedUser(
        user_id="viewer-1",
        organization_id="org-123",
        email="viewer@example.com",
        roles=["viewer"],
        permissions=permissions,
        token_jti="token-viewer",
    )


@pytest.fixture
def sample_dev_users():
    """Create sample dev users for mocking."""
    now = datetime.now(timezone.utc)
    return [
        DevUser(
            user_id="user-1",
            username="admin@example.com",
            email="admin@example.com",
            display_name="Admin User",
            created_at=now,
            is_active=True,
            roles=["admin"],
        ),
        DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=now,
            is_active=True,
            roles=["member"],
        ),
        DevUser(
            user_id="user-3",
            username="viewer@example.com",
            email="viewer@example.com",
            display_name="Viewer User",
            created_at=now,
            is_active=True,
            roles=["viewer"],
        ),
    ]


@pytest.fixture
def mock_auth_and_service(admin_user, sample_dev_users):
    """Mock authentication and user service."""
    with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
         patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service, \
         patch("faultmaven.api.v1.auth_dependencies.get_user_store") as mock_get_store:

        # Mock auth service
        mock_auth = MagicMock()
        mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(return_value=admin_user)
        mock_get_auth.return_value = mock_auth

        # Mock user service
        mock_service = MagicMock()
        mock_service.list_users = AsyncMock(return_value=(sample_dev_users, len(sample_dev_users)))
        mock_service.get_user_with_metadata = AsyncMock()
        mock_service.activate_user = AsyncMock()
        mock_service.deactivate_user = AsyncMock()
        mock_service.assign_role = AsyncMock()
        mock_service.remove_role = AsyncMock()
        mock_get_service.return_value = mock_service

        # Mock user store
        mock_store = MagicMock()
        mock_get_store.return_value = mock_store

        yield {
            "auth_service": mock_auth,
            "user_service": mock_service,
            "user_store": mock_store,
        }


# ============================================================
# GET /api/v1/admin/users Tests
# ============================================================


class TestListUsersEndpoint:
    """Tests for GET /api/v1/admin/users."""

    def test_returns_200_ok_with_user_list(self, client, mock_auth_and_service):
        """200 OK returns user list (admin)."""
        response = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data

    def test_returns_pagination_info(self, client, mock_auth_and_service):
        """Returns pagination info (total, limit, offset)."""
        response = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert data["limit"] == 50
        assert data["offset"] == 0

    def test_filter_by_is_active(self, client, mock_auth_and_service):
        """Filter by is_active works."""
        response = client.get(
            "/api/v1/admin/users?is_active=true",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        # Verify the service was called with the filter
        mock_auth_and_service["user_service"].list_users.assert_called_once()
        call_kwargs = mock_auth_and_service["user_service"].list_users.call_args[1]
        assert call_kwargs["is_active"] is True

    def test_filter_by_role(self, client, mock_auth_and_service):
        """Filter by role works."""
        response = client.get(
            "/api/v1/admin/users?role=admin",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        call_kwargs = mock_auth_and_service["user_service"].list_users.call_args[1]
        assert call_kwargs["role"] == "admin"

    def test_search_by_email(self, client, mock_auth_and_service):
        """Search by email works."""
        response = client.get(
            "/api/v1/admin/users?search=admin",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        call_kwargs = mock_auth_and_service["user_service"].list_users.call_args[1]
        assert call_kwargs["search"] == "admin"

    def test_pagination_limit_offset(self, client, mock_auth_and_service):
        """Pagination (limit/offset) works."""
        response = client.get(
            "/api/v1/admin/users?limit=10&offset=5",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        call_kwargs = mock_auth_and_service["user_service"].list_users.call_args[1]
        assert call_kwargs["limit"] == 10
        assert call_kwargs["offset"] == 5

    def test_401_unauthorized_if_no_token(self, client):
        """401 Unauthorized if no JWT token."""
        response = client.get("/api/v1/admin/users")

        assert response.status_code == 401

    def test_403_forbidden_if_not_admin(self, client, member_user, mock_auth_and_service):
        """403 Forbidden if not admin (member, viewer)."""
        mock_auth_and_service["auth_service"].extract_user_from_token_with_revocation_check.return_value = member_user

        response = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403


# ============================================================
# GET /api/v1/admin/users/{user_id} Tests
# ============================================================


class TestGetUserDetailsEndpoint:
    """Tests for GET /api/v1/admin/users/{user_id}."""

    def test_returns_200_ok_with_user_details(self, client, mock_auth_and_service):
        """200 OK returns user details (admin)."""
        mock_auth_and_service["user_service"].get_user_with_metadata.return_value = {
            "user_id": "user-1",
            "organization_id": "org-123",
            "email": "admin@example.com",
            "full_name": "Admin User",
            "roles": ["admin"],
            "permissions": ["cases:read"],
            "is_active": True,
            "is_verified": True,
            "last_login_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "metadata": {"login_count": 0},
        }

        response = client.get(
            "/api/v1/admin/users/user-1",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "user-1"
        assert "permissions" in data
        assert "metadata" in data

    def test_includes_derived_permissions(self, client, mock_auth_and_service):
        """Includes derived permissions."""
        mock_auth_and_service["user_service"].get_user_with_metadata.return_value = {
            "user_id": "user-1",
            "organization_id": "org-123",
            "email": "admin@example.com",
            "full_name": "Admin User",
            "roles": ["admin"],
            "permissions": ["cases:read", "cases:write", "sessions:read"],
            "is_active": True,
            "is_verified": True,
            "last_login_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "metadata": {},
        }

        response = client.get(
            "/api/v1/admin/users/user-1",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["permissions"]) > 0
        assert "cases:read" in data["permissions"]

    def test_404_not_found_if_user_doesnt_exist(self, client, mock_auth_and_service):
        """404 Not Found if user doesn't exist."""
        mock_auth_and_service["user_service"].get_user_with_metadata.return_value = None

        response = client.get(
            "/api/v1/admin/users/nonexistent",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 404

    def test_401_unauthorized_if_no_token(self, client):
        """401 Unauthorized if no JWT token."""
        response = client.get("/api/v1/admin/users/user-1")

        assert response.status_code == 401

    def test_403_forbidden_if_not_admin(self, client, member_user, mock_auth_and_service):
        """403 Forbidden if not admin."""
        mock_auth_and_service["auth_service"].extract_user_from_token_with_revocation_check.return_value = member_user

        response = client.get(
            "/api/v1/admin/users/user-1",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403


# ============================================================
# POST /api/v1/admin/users/{user_id}/deactivate Tests
# ============================================================


class TestDeactivateUserEndpoint:
    """Tests for POST /api/v1/admin/users/{user_id}/deactivate."""

    def test_returns_200_ok_deactivates_user(self, client, mock_auth_and_service, sample_dev_users):
        """200 OK deactivates user (admin)."""
        deactivated_user = DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=datetime.now(timezone.utc),
            is_active=False,
            roles=["member"],
        )
        mock_auth_and_service["user_service"].deactivate_user.return_value = deactivated_user

        response = client.post(
            "/api/v1/admin/users/user-2/deactivate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "user-2"
        assert data["is_active"] is False
        assert "message" in data

    def test_message_confirms_token_revocation(self, client, mock_auth_and_service, sample_dev_users):
        """Message confirms token revocation."""
        deactivated_user = DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=datetime.now(timezone.utc),
            is_active=False,
            roles=["member"],
        )
        mock_auth_and_service["user_service"].deactivate_user.return_value = deactivated_user

        response = client.post(
            "/api/v1/admin/users/user-2/deactivate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "token" in data["message"].lower() or "revoked" in data["message"].lower()

    def test_403_forbidden_when_deactivating_self(self, client, mock_auth_and_service):
        """403 Forbidden when admin tries to deactivate self."""
        from faultmaven.exceptions import AuthorizationError
        mock_auth_and_service["user_service"].deactivate_user.side_effect = AuthorizationError(
            "Cannot deactivate your own account"
        )

        response = client.post(
            "/api/v1/admin/users/admin-1/deactivate",  # Same as current user
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403

    def test_404_not_found_if_user_doesnt_exist(self, client, mock_auth_and_service):
        """404 Not Found if user doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_auth_and_service["user_service"].deactivate_user.side_effect = NotFoundError("User", "nonexistent")

        response = client.post(
            "/api/v1/admin/users/nonexistent/deactivate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 404

    def test_409_conflict_if_already_deactivated(self, client, mock_auth_and_service):
        """409 Conflict if already deactivated."""
        from faultmaven.exceptions import ConflictError
        mock_auth_and_service["user_service"].deactivate_user.side_effect = ConflictError(
            "User is already deactivated"
        )

        response = client.post(
            "/api/v1/admin/users/user-2/deactivate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 409

    def test_401_unauthorized_if_no_token(self, client):
        """401 Unauthorized if no JWT token."""
        response = client.post("/api/v1/admin/users/user-1/deactivate")

        assert response.status_code == 401


# ============================================================
# POST /api/v1/admin/users/{user_id}/activate Tests
# ============================================================


class TestActivateUserEndpoint:
    """Tests for POST /api/v1/admin/users/{user_id}/activate."""

    def test_returns_200_ok_activates_user(self, client, mock_auth_and_service):
        """200 OK activates user (admin)."""
        activated_user = DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["member"],
        )
        mock_auth_and_service["user_service"].activate_user.return_value = activated_user

        response = client.post(
            "/api/v1/admin/users/user-2/activate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "user-2"
        assert data["is_active"] is True

    def test_404_not_found_if_user_doesnt_exist(self, client, mock_auth_and_service):
        """404 Not Found if user doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_auth_and_service["user_service"].activate_user.side_effect = NotFoundError("User", "nonexistent")

        response = client.post(
            "/api/v1/admin/users/nonexistent/activate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 404

    def test_409_conflict_if_already_active(self, client, mock_auth_and_service):
        """409 Conflict if already active."""
        from faultmaven.exceptions import ConflictError
        mock_auth_and_service["user_service"].activate_user.side_effect = ConflictError(
            "User is already active"
        )

        response = client.post(
            "/api/v1/admin/users/user-2/activate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 409

    def test_401_unauthorized_if_no_token(self, client):
        """401 Unauthorized if no JWT token."""
        response = client.post("/api/v1/admin/users/user-1/activate")

        assert response.status_code == 401

    def test_403_forbidden_if_not_admin(self, client, member_user, mock_auth_and_service):
        """403 Forbidden if not admin."""
        mock_auth_and_service["auth_service"].extract_user_from_token_with_revocation_check.return_value = member_user

        response = client.post(
            "/api/v1/admin/users/user-1/activate",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403


# ============================================================
# POST /api/v1/admin/users/{user_id}/roles Tests
# ============================================================


class TestAssignRoleEndpoint:
    """Tests for POST /api/v1/admin/users/{user_id}/roles."""

    def test_returns_200_ok_assigns_admin_role(self, client, mock_auth_and_service):
        """200 OK assigns admin role."""
        updated_user = DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["admin"],
        )
        mock_auth_and_service["user_service"].assign_role.return_value = updated_user

        response = client.post(
            "/api/v1/admin/users/user-2/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "admin" in data["roles"]

    def test_returns_200_ok_assigns_member_role(self, client, mock_auth_and_service):
        """200 OK assigns member role."""
        updated_user = DevUser(
            user_id="user-3",
            username="viewer@example.com",
            email="viewer@example.com",
            display_name="Viewer User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["member"],
        )
        mock_auth_and_service["user_service"].assign_role.return_value = updated_user

        response = client.post(
            "/api/v1/admin/users/user-3/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "member"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "member" in data["roles"]

    def test_returns_200_ok_assigns_viewer_role(self, client, mock_auth_and_service):
        """200 OK assigns viewer role."""
        updated_user = DevUser(
            user_id="user-1",
            username="admin@example.com",
            email="admin@example.com",
            display_name="Admin User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["viewer"],
        )
        mock_auth_and_service["user_service"].assign_role.return_value = updated_user

        response = client.post(
            "/api/v1/admin/users/user-1/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "viewer"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "viewer" in data["roles"]

    def test_message_confirms_token_revocation(self, client, mock_auth_and_service):
        """Message confirms token revocation."""
        updated_user = DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["admin"],
        )
        mock_auth_and_service["user_service"].assign_role.return_value = updated_user

        response = client.post(
            "/api/v1/admin/users/user-2/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "token" in data["message"].lower() or "revoked" in data["message"].lower()

    def test_403_forbidden_when_modifying_own_roles(self, client, mock_auth_and_service):
        """403 Forbidden when admin tries to modify own roles."""
        from faultmaven.exceptions import AuthorizationError
        mock_auth_and_service["user_service"].assign_role.side_effect = AuthorizationError(
            "Cannot modify your own roles"
        )

        response = client.post(
            "/api/v1/admin/users/admin-1/roles",  # Same as current user
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "viewer"},
        )

        assert response.status_code == 403

    def test_404_not_found_if_user_doesnt_exist(self, client, mock_auth_and_service):
        """404 Not Found if user doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_auth_and_service["user_service"].assign_role.side_effect = NotFoundError("User", "nonexistent")

        response = client.post(
            "/api/v1/admin/users/nonexistent/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"},
        )

        assert response.status_code == 404

    def test_422_unprocessable_entity_on_invalid_role(self, client, mock_auth_and_service):
        """422 Unprocessable Entity on invalid role."""
        response = client.post(
            "/api/v1/admin/users/user-2/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "superadmin"},  # Invalid
        )

        assert response.status_code == 422

    def test_409_conflict_if_already_has_role(self, client, mock_auth_and_service):
        """409 Conflict if already has this role."""
        from faultmaven.exceptions import ConflictError
        mock_auth_and_service["user_service"].assign_role.side_effect = ConflictError(
            "User already has role 'admin'"
        )

        response = client.post(
            "/api/v1/admin/users/user-1/roles",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"},
        )

        assert response.status_code == 409

    def test_401_unauthorized_if_no_token(self, client):
        """401 Unauthorized if no JWT token."""
        response = client.post(
            "/api/v1/admin/users/user-1/roles",
            json={"role": "admin"},
        )

        assert response.status_code == 401


# ============================================================
# DELETE /api/v1/admin/users/{user_id}/roles/{role} Tests
# ============================================================


class TestRemoveRoleEndpoint:
    """Tests for DELETE /api/v1/admin/users/{user_id}/roles/{role}."""

    def test_returns_200_ok_removes_admin_role(self, client, mock_auth_and_service):
        """200 OK removes admin role, downgrades to viewer."""
        downgraded_user = DevUser(
            user_id="user-1",
            username="admin@example.com",
            email="admin@example.com",
            display_name="Admin User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["viewer"],
        )
        mock_auth_and_service["user_service"].remove_role.return_value = downgraded_user

        response = client.delete(
            "/api/v1/admin/users/user-1/roles/admin",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "viewer" in data["roles"]

    def test_returns_200_ok_removes_member_role(self, client, mock_auth_and_service):
        """200 OK removes member role, downgrades to viewer."""
        downgraded_user = DevUser(
            user_id="user-2",
            username="member@example.com",
            email="member@example.com",
            display_name="Member User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["viewer"],
        )
        mock_auth_and_service["user_service"].remove_role.return_value = downgraded_user

        response = client.delete(
            "/api/v1/admin/users/user-2/roles/member",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "viewer" in data["roles"]

    def test_message_confirms_token_revocation(self, client, mock_auth_and_service):
        """Message confirms token revocation."""
        downgraded_user = DevUser(
            user_id="user-1",
            username="admin@example.com",
            email="admin@example.com",
            display_name="Admin User",
            created_at=datetime.now(timezone.utc),
            is_active=True,
            roles=["viewer"],
        )
        mock_auth_and_service["user_service"].remove_role.return_value = downgraded_user

        response = client.delete(
            "/api/v1/admin/users/user-1/roles/admin",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "token" in data["message"].lower() or "revoked" in data["message"].lower()

    def test_403_forbidden_when_removing_own_admin_role(self, client, mock_auth_and_service):
        """403 Forbidden when admin tries to remove own admin role."""
        from faultmaven.exceptions import AuthorizationError
        mock_auth_and_service["user_service"].remove_role.side_effect = AuthorizationError(
            "Cannot modify your own roles"
        )

        response = client.delete(
            "/api/v1/admin/users/admin-1/roles/admin",  # Same as current user
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403

    def test_404_not_found_if_user_doesnt_exist(self, client, mock_auth_and_service):
        """404 Not Found if user doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_auth_and_service["user_service"].remove_role.side_effect = NotFoundError("User", "nonexistent")

        response = client.delete(
            "/api/v1/admin/users/nonexistent/roles/admin",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 404

    def test_404_not_found_if_user_doesnt_have_role(self, client, mock_auth_and_service):
        """404 Not Found if user doesn't have this role."""
        from faultmaven.exceptions import NotFoundError
        mock_auth_and_service["user_service"].remove_role.side_effect = NotFoundError(
            "Role", "user-3/admin"
        )

        response = client.delete(
            "/api/v1/admin/users/user-3/roles/admin",  # Viewer doesn't have admin
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 404

    def test_422_unprocessable_entity_when_removing_viewer_role(self, client, mock_auth_and_service):
        """422 Unprocessable Entity when removing viewer role."""
        from faultmaven.exceptions import ValidationException
        mock_auth_and_service["user_service"].remove_role.side_effect = ValidationException(
            "Cannot remove viewer role"
        )

        response = client.delete(
            "/api/v1/admin/users/user-3/roles/viewer",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 422

    def test_401_unauthorized_if_no_token(self, client):
        """401 Unauthorized if no JWT token."""
        response = client.delete("/api/v1/admin/users/user-1/roles/admin")

        assert response.status_code == 401

    def test_403_forbidden_if_not_admin(self, client, member_user, mock_auth_and_service):
        """403 Forbidden if not admin."""
        mock_auth_and_service["auth_service"].extract_user_from_token_with_revocation_check.return_value = member_user

        response = client.delete(
            "/api/v1/admin/users/user-1/roles/admin",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403
