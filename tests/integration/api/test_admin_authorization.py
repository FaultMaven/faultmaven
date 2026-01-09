"""Authorization Tests for Admin Endpoints (TASK-019)

Tests admin authorization controls:
1. Admin-Only Enforcement - Admin can access, member/viewer cannot
2. Self-Modification Prevention - Admin cannot modify themselves
3. Organization Boundary Enforcement - Admin cannot access other org's users

Coverage Target: 90%+
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.models.auth import AuthenticatedUser, DevUser
from faultmaven.models.rbac import get_permissions_for_roles


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def app():
    """Create FastAPI test application."""
    return main_app


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
def other_org_admin():
    """Create admin in different organization."""
    permissions = [p.value for p in get_permissions_for_roles(["admin"])]
    return AuthenticatedUser(
        user_id="admin-org-b",
        organization_id="org-other",  # Different org
        email="admin@other.com",
        roles=["admin"],
        permissions=permissions,
        token_jti="token-admin-other",
    )


@pytest.fixture
def sample_dev_users():
    """Create sample dev users."""
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
    ]


def create_mock_auth(user):
    """Create mock auth service returning the given user."""
    mock_auth = MagicMock()
    mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(return_value=user)
    return mock_auth


def create_mock_user_service(sample_dev_users):
    """Create mock user service."""
    mock_service = MagicMock()
    mock_service.list_users = AsyncMock(return_value=(sample_dev_users, len(sample_dev_users)))
    mock_service.get_user_with_metadata = AsyncMock(return_value={
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
        "metadata": {},
    })
    mock_service.activate_user = AsyncMock(return_value=sample_dev_users[0])
    mock_service.deactivate_user = AsyncMock(return_value=sample_dev_users[0])
    mock_service.assign_role = AsyncMock(return_value=sample_dev_users[0])
    mock_service.remove_role = AsyncMock(return_value=sample_dev_users[0])
    return mock_service


# ============================================================
# Admin-Only Enforcement Tests
# ============================================================


class TestAdminOnlyEnforcement:
    """Tests that admin endpoints are admin-only."""

    def test_admin_can_list_users(self, client, admin_user, sample_dev_users):
        """Admin can list users."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_get_service.return_value = create_mock_user_service(sample_dev_users)

            response = client.get(
                "/api/v1/admin/users",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200

    def test_member_cannot_list_users(self, client, member_user, sample_dev_users):
        """Member CANNOT list users (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(member_user)

            response = client.get(
                "/api/v1/admin/users",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_viewer_cannot_list_users(self, client, viewer_user, sample_dev_users):
        """Viewer CANNOT list users (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(viewer_user)

            response = client.get(
                "/api/v1/admin/users",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_admin_can_view_user_details(self, client, admin_user, sample_dev_users):
        """Admin can view user details."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_get_service.return_value = create_mock_user_service(sample_dev_users)

            response = client.get(
                "/api/v1/admin/users/user-1",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200

    def test_member_cannot_view_user_details(self, client, member_user):
        """Member CANNOT view user details (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(member_user)

            response = client.get(
                "/api/v1/admin/users/user-1",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_admin_can_deactivate_users(self, client, admin_user, sample_dev_users):
        """Admin can deactivate users."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.deactivate_user.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=False,
                roles=["member"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/user-2/deactivate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200

    def test_member_cannot_deactivate_users(self, client, member_user):
        """Member CANNOT deactivate users (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(member_user)

            response = client.post(
                "/api/v1/admin/users/user-1/deactivate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_admin_can_activate_users(self, client, admin_user, sample_dev_users):
        """Admin can activate users."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_get_service.return_value = create_mock_user_service(sample_dev_users)

            response = client.post(
                "/api/v1/admin/users/user-2/activate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200

    def test_member_cannot_activate_users(self, client, member_user):
        """Member CANNOT activate users (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(member_user)

            response = client.post(
                "/api/v1/admin/users/user-1/activate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_admin_can_assign_roles(self, client, admin_user, sample_dev_users):
        """Admin can assign roles."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.assign_role.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=True,
                roles=["admin"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/user-2/roles",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "admin"},
            )

            assert response.status_code == 200

    def test_member_cannot_assign_roles(self, client, member_user):
        """Member CANNOT assign roles (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(member_user)

            response = client.post(
                "/api/v1/admin/users/user-1/roles",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "viewer"},
            )

            assert response.status_code == 403

    def test_admin_can_remove_roles(self, client, admin_user, sample_dev_users):
        """Admin can remove roles."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.remove_role.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=True,
                roles=["viewer"],
            )
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/admin/users/user-2/roles/member",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200

    def test_member_cannot_remove_roles(self, client, member_user):
        """Member CANNOT remove roles (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
            mock_get_auth.return_value = create_mock_auth(member_user)

            response = client.delete(
                "/api/v1/admin/users/user-1/roles/admin",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403


# ============================================================
# Self-Modification Prevention Tests
# ============================================================


class TestSelfModificationPrevention:
    """Tests that admins cannot modify themselves."""

    def test_admin_cannot_deactivate_self(self, client, admin_user, sample_dev_users):
        """Admin CANNOT deactivate self (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            from faultmaven.exceptions import AuthorizationError
            mock_service.deactivate_user.side_effect = AuthorizationError("Cannot deactivate your own account")
            mock_get_service.return_value = mock_service

            response = client.post(
                f"/api/v1/admin/users/{admin_user.user_id}/deactivate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_admin_cannot_modify_own_roles(self, client, admin_user, sample_dev_users):
        """Admin CANNOT modify own roles (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            from faultmaven.exceptions import AuthorizationError
            mock_service.assign_role.side_effect = AuthorizationError("Cannot modify your own roles")
            mock_get_service.return_value = mock_service

            response = client.post(
                f"/api/v1/admin/users/{admin_user.user_id}/roles",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "viewer"},
            )

            assert response.status_code == 403

    def test_admin_cannot_remove_own_admin_role(self, client, admin_user, sample_dev_users):
        """Admin CANNOT remove own admin role (403)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            from faultmaven.exceptions import AuthorizationError
            mock_service.remove_role.side_effect = AuthorizationError("Cannot modify your own roles")
            mock_get_service.return_value = mock_service

            response = client.delete(
                f"/api/v1/admin/users/{admin_user.user_id}/roles/admin",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 403

    def test_admin_can_deactivate_other_admins(self, client, admin_user, sample_dev_users):
        """Admin CAN deactivate other admins."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            # Return a deactivated user
            mock_service.deactivate_user.return_value = DevUser(
                user_id="other-admin",
                username="other-admin@example.com",
                email="other-admin@example.com",
                display_name="Other Admin",
                created_at=datetime.now(timezone.utc),
                is_active=False,
                roles=["admin"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/other-admin/deactivate",  # Different from current user
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200

    def test_admin_can_modify_other_admins_roles(self, client, admin_user, sample_dev_users):
        """Admin CAN modify other admins' roles."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.assign_role.return_value = DevUser(
                user_id="other-admin",
                username="other-admin@example.com",
                email="other-admin@example.com",
                display_name="Other Admin",
                created_at=datetime.now(timezone.utc),
                is_active=True,
                roles=["viewer"],  # Downgraded
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/other-admin/roles",  # Different from current user
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "viewer"},
            )

            assert response.status_code == 200


# ============================================================
# Organization Boundary Tests
# ============================================================


class TestOrganizationBoundaryEnforcement:
    """Tests that admins cannot access users in other organizations."""

    def test_list_users_filters_by_organization(self, client, admin_user, sample_dev_users):
        """List users is filtered by organization."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/admin/users",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200
            # Verify list_users was called with the user's organization_id
            mock_service.list_users.assert_called_once()
            call_kwargs = mock_service.list_users.call_args[1]
            assert call_kwargs["organization_id"] == admin_user.organization_id

    def test_get_user_details_uses_organization_filter(self, client, admin_user, sample_dev_users):
        """Get user details passes organization_id for filtering."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/admin/users/user-1",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200
            # Verify get_user_with_metadata was called with the user's organization_id
            mock_service.get_user_with_metadata.assert_called_once()
            call_kwargs = mock_service.get_user_with_metadata.call_args[1]
            assert call_kwargs["organization_id"] == admin_user.organization_id

    def test_deactivate_user_uses_organization_filter(self, client, admin_user, sample_dev_users):
        """Deactivate user passes organization_id for filtering."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.deactivate_user.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=False,
                roles=["member"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/user-2/deactivate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200
            # Verify deactivate_user was called with the user's organization_id
            mock_service.deactivate_user.assert_called_once()
            call_kwargs = mock_service.deactivate_user.call_args[1]
            assert call_kwargs["organization_id"] == admin_user.organization_id

    def test_assign_role_uses_organization_filter(self, client, admin_user, sample_dev_users):
        """Assign role passes organization_id for filtering."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.assign_role.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=True,
                roles=["admin"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/user-2/roles",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "admin"},
            )

            assert response.status_code == 200
            # Verify assign_role was called with the user's organization_id
            mock_service.assign_role.assert_called_once()
            call_kwargs = mock_service.assign_role.call_args[1]
            assert call_kwargs["organization_id"] == admin_user.organization_id


# ============================================================
# Token Revocation Tests
# ============================================================


class TestTokenRevocation:
    """Tests that token revocation is triggered on user state changes."""

    def test_deactivation_revokes_tokens(self, client, admin_user, sample_dev_users):
        """User deactivation revokes all JWT tokens."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.deactivate_user.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=False,
                roles=["member"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/user-2/deactivate",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200
            # Message should mention token revocation
            data = response.json()
            assert "token" in data["message"].lower() or "revoked" in data["message"].lower()

    def test_role_assignment_revokes_tokens(self, client, admin_user, sample_dev_users):
        """Role assignment revokes all JWT tokens."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.assign_role.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=True,
                roles=["admin"],
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/admin/users/user-2/roles",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "admin"},
            )

            assert response.status_code == 200
            data = response.json()
            assert "token" in data["message"].lower() or "revoked" in data["message"].lower()

    def test_role_removal_revokes_tokens(self, client, admin_user, sample_dev_users):
        """Role removal revokes all JWT tokens."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.routes.admin.get_user_service_with_store") as mock_get_service:

            mock_get_auth.return_value = create_mock_auth(admin_user)
            mock_service = create_mock_user_service(sample_dev_users)
            mock_service.remove_role.return_value = DevUser(
                user_id="user-2",
                username="member@example.com",
                email="member@example.com",
                display_name="Member User",
                created_at=datetime.now(timezone.utc),
                is_active=True,
                roles=["viewer"],
            )
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/admin/users/user-2/roles/member",
                headers={"Authorization": "Bearer valid-token"},
            )

            assert response.status_code == 200
            data = response.json()
            assert "token" in data["message"].lower() or "revoked" in data["message"].lower()
