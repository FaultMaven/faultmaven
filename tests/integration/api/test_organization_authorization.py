"""Authorization tests for Organization Management API (TASK-021)

Tests role-based access control for organization endpoints:
- Owner has full access
- Admin has limited access (no update/delete org, no update settings, no change roles)
- Member has read-only access
- Non-member has no access

Coverage Target: 20-25 tests
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.exceptions import AuthorizationError, NotFoundError
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.models.interfaces_user import Organization, OrganizationMember, OrgPlanTier
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


def create_user(user_id: str, role: str, org_role: str = None):
    """Create authenticated user with role."""
    permissions = [p.value for p in get_permissions_for_roles([role])]
    return AuthenticatedUser(
        user_id=user_id,
        organization_id="org-123",
        email=f"{role}@example.com",
        roles=[role],
        permissions=permissions,
        token_jti=f"token-{user_id}",
    )


@pytest.fixture
def owner_user():
    """Create owner authenticated user."""
    return create_user("user-owner", "admin")


@pytest.fixture
def admin_user():
    """Create admin authenticated user."""
    return create_user("user-admin", "admin")


@pytest.fixture
def member_user():
    """Create member authenticated user."""
    return create_user("user-member", "member")


@pytest.fixture
def non_member_user():
    """Create non-member authenticated user."""
    return create_user("user-nonmember", "viewer")


@pytest.fixture
def sample_organization():
    """Create sample organization."""
    now = datetime.now(timezone.utc)
    return Organization(
        org_id="org-123",
        name="Test Organization",
        slug="test-org",
        description="Test description",
        plan_tier=OrgPlanTier.PRO,
        max_members=50,
        settings={"allow_public_cases": False},
        created_at=now,
        updated_at=now,
    )


# ============================================================
# Organization-Level Authorization Tests
# ============================================================


class TestOwnerFullAccess:
    """Tests that owner has full access to all endpoints."""

    def test_owner_can_view_organization(self, client, owner_user, sample_organization):
        """Owner can view organization details."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization = AsyncMock(return_value=sample_organization)
            mock_service.organization_service = MagicMock()
            mock_service.organization_service.list_organization_members = AsyncMock(return_value=[])
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 200

    def test_owner_can_update_organization(self, client, owner_user, sample_organization):
        """Owner can update organization."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_organization = AsyncMock(return_value=sample_organization)
            mock_service.organization_service = MagicMock()
            mock_service.organization_service.list_organization_members = AsyncMock(return_value=[])
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"},
                json={"name": "Updated Name"}
            )

            assert response.status_code == 200

    def test_owner_can_delete_organization(self, client, owner_user):
        """Owner can delete organization."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.delete_organization = AsyncMock(return_value=True)
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 200

    def test_owner_can_update_settings(self, client, owner_user):
        """Owner can update settings."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_organization_settings = AsyncMock(return_value={
                "organization_id": "org-123",
                "settings": {"allow_public_cases": True},
                "updated_at": datetime.now(timezone.utc)
            })
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-123/settings",
                headers={"Authorization": "Bearer valid-token"},
                json={"allow_public_cases": True}
            )

            assert response.status_code == 200

    def test_owner_can_change_member_roles(self, client, owner_user):
        """Owner can change member roles."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_member_role = AsyncMock(return_value={
                "user_id": "user-member",
                "email": "member@test.com",
                "full_name": "Member",
                "role": "admin",
                "joined_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            })
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-123/members/user-member",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "admin"}
            )

            assert response.status_code == 200


class TestAdminLimitedAccess:
    """Tests that admin has limited access."""

    def test_admin_can_view_organization(self, client, admin_user, sample_organization):
        """Admin can view organization details."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=admin_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization = AsyncMock(return_value=sample_organization)
            mock_service.organization_service = MagicMock()
            mock_service.organization_service.list_organization_members = AsyncMock(return_value=[])
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 200

    def test_admin_cannot_update_organization(self, client, admin_user):
        """Admin cannot update organization."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=admin_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_organization = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"},
                json={"name": "Updated Name"}
            )

            assert response.status_code == 403

    def test_admin_cannot_delete_organization(self, client, admin_user):
        """Admin cannot delete organization."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=admin_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.delete_organization = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_admin_cannot_update_settings(self, client, admin_user):
        """Admin cannot update settings."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=admin_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_organization_settings = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-123/settings",
                headers={"Authorization": "Bearer valid-token"},
                json={"allow_public_cases": True}
            )

            assert response.status_code == 403

    def test_admin_cannot_change_member_roles(self, client, admin_user):
        """Admin cannot change member roles."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=admin_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_member_role = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-123/members/user-member",
                headers={"Authorization": "Bearer valid-token"},
                json={"role": "admin"}
            )

            assert response.status_code == 403

    def test_admin_can_add_members_with_member_role(self, client, admin_user):
        """Admin can add members (with member role only)."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=admin_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.add_member = AsyncMock(return_value={
                "user_id": "user-new",
                "email": "new@test.com",
                "full_name": "New User",
                "role": "member",
                "joined_at": datetime.now(timezone.utc),
                "invitation_sent": True
            })
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"},
                json={"email": "new@test.com", "role": "member"}
            )

            assert response.status_code == 201


class TestMemberReadOnlyAccess:
    """Tests that member has read-only access."""

    def test_member_can_view_organization(self, client, member_user, sample_organization):
        """Member can view organization details."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization = AsyncMock(return_value=sample_organization)
            mock_service.organization_service = MagicMock()
            mock_service.organization_service.list_organization_members = AsyncMock(return_value=[])
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 200

    def test_member_can_list_members(self, client, member_user):
        """Member can list members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.list_organization_members = AsyncMock(return_value=([], 0))
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 200

    def test_member_can_view_settings(self, client, member_user):
        """Member can view settings."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization_settings = AsyncMock(return_value={
                "organization_id": "org-123",
                "plan_tier": "pro",
                "max_members": 50,
                "current_member_count": 3,
                "max_cases_per_month": 500,
                "max_storage_gb": 100,
                "features": {},
                "settings": {}
            })
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123/settings",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 200

    def test_member_cannot_add_members(self, client, member_user):
        """Member cannot add members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.add_member = AsyncMock(
                side_effect=AuthorizationError("Organization admin access required")
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"},
                json={"email": "new@test.com", "role": "member"}
            )

            assert response.status_code == 403

    def test_member_cannot_remove_members(self, client, member_user):
        """Member cannot remove members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.remove_member = AsyncMock(
                side_effect=AuthorizationError("Organization admin access required")
            )
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/organizations/org-123/members/user-other",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403


class TestNonMemberNoAccess:
    """Tests that non-member has no access."""

    def test_non_member_cannot_view_organization(self, client, non_member_user):
        """Non-member cannot view organization."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=non_member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization = AsyncMock(
                side_effect=AuthorizationError("Organization membership required")
            )
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_non_member_cannot_list_members(self, client, non_member_user):
        """Non-member cannot list members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=non_member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.list_organization_members = AsyncMock(
                side_effect=AuthorizationError("Organization membership required")
            )
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_non_member_cannot_view_settings(self, client, non_member_user):
        """Non-member cannot view settings."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=non_member_user
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization_settings = AsyncMock(
                side_effect=AuthorizationError("Organization membership required")
            )
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-123/settings",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403


# ============================================================
# Plan Tier Limit Tests
# ============================================================


class TestPlanTierLimits:
    """Tests for plan tier limits."""

    def test_free_plan_max_5_members(self, client, owner_user):
        """Free plan: max 5 members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            from faultmaven.exceptions import ConflictError
            mock_service = MagicMock()
            mock_service.add_member = AsyncMock(
                side_effect=ConflictError("Organization has reached maximum member limit (5)")
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"},
                json={"email": "new@test.com", "role": "member"}
            )

            # Max members returns 403 (Forbidden due to limit)
            assert response.status_code == 403

    def test_pro_plan_max_50_members(self, client, owner_user):
        """Pro plan: max 50 members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            from faultmaven.exceptions import ConflictError
            mock_service = MagicMock()
            mock_service.add_member = AsyncMock(
                side_effect=ConflictError("Organization has reached maximum member limit (50)")
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"},
                json={"email": "new@test.com", "role": "member"}
            )

            # Max members returns 403 (Forbidden due to limit)
            assert response.status_code == 403

    def test_adding_member_beyond_limit_returns_403(self, client, owner_user):
        """Adding member beyond limit returns 403."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=owner_user
            )
            mock_get_auth.return_value = mock_auth

            from faultmaven.exceptions import ConflictError
            mock_service = MagicMock()
            mock_service.add_member = AsyncMock(
                side_effect=ConflictError("max_members limit reached")
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/organizations/org-123/members",
                headers={"Authorization": "Bearer valid-token"},
                json={"email": "new@test.com", "role": "member"}
            )

            assert response.status_code == 403
