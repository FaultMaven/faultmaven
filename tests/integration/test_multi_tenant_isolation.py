"""Multi-Tenant Isolation Tests (TASK-021)

Tests that organization data is properly isolated:
- Users can only access their organization's data
- No cross-organization data leakage
- list_organizations only shows user's memberships
- All organization endpoints enforce membership checks

Coverage Target: 15-20 tests
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.api.services.organization_api_service import (
    APIOrganizationService,
    ROLE_OWNER,
    ROLE_ADMIN,
    ROLE_MEMBER,
)
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


@pytest.fixture
def org_a():
    """Create organization A."""
    now = datetime.now(timezone.utc)
    return Organization(
        org_id="org-a",
        name="Organization A",
        slug="org-a",
        description="Organization A description",
        plan_tier=OrgPlanTier.PRO,
        max_members=50,
        settings={},
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def org_b():
    """Create organization B."""
    now = datetime.now(timezone.utc)
    return Organization(
        org_id="org-b",
        name="Organization B",
        slug="org-b",
        description="Organization B description",
        plan_tier=OrgPlanTier.FREE,
        max_members=5,
        settings={},
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def user_a():
    """Create user A (member of org A)."""
    permissions = [p.value for p in get_permissions_for_roles(["admin"])]
    return AuthenticatedUser(
        user_id="user-a",
        organization_id="org-a",
        email="user-a@org-a.com",
        roles=["admin"],
        permissions=permissions,
        token_jti="token-user-a",
    )


@pytest.fixture
def user_b():
    """Create user B (member of org B)."""
    permissions = [p.value for p in get_permissions_for_roles(["admin"])]
    return AuthenticatedUser(
        user_id="user-b",
        organization_id="org-b",
        email="user-b@org-b.com",
        roles=["admin"],
        permissions=permissions,
        token_jti="token-user-b",
    )


# ============================================================
# Data Isolation Tests - API Service Layer
# ============================================================


class TestOrganizationDataIsolation:
    """Tests that organization data is properly isolated at the API service layer."""

    @pytest.mark.asyncio
    async def test_user_a_cannot_access_org_b(self, org_a, org_b):
        """User A cannot access User B's organization."""
        mock_org_service = MagicMock()
        mock_org_service.get_organization = AsyncMock(return_value=org_b)
        mock_org_service.get_member_role = AsyncMock(return_value=None)  # User A not member of org B

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError) as exc_info:
            await api_service.get_organization(
                organization_id="org-b",
                user_id="user-a",
            )

        assert "membership required" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_list_organizations_only_shows_user_memberships(self, org_a, org_b):
        """list_organizations only shows user's memberships."""
        now = datetime.now(timezone.utc)
        member_a = OrganizationMember(
            user_id="user-a",
            org_id="org-a",
            role_id=ROLE_OWNER,
            joined_at=now,
        )

        mock_org_service = MagicMock()
        # User A is only member of org A
        mock_org_service.list_user_organizations = AsyncMock(return_value=[org_a])
        mock_org_service.get_member_role = AsyncMock(return_value=ROLE_OWNER)
        mock_org_service.list_organization_members = AsyncMock(return_value=[member_a])

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        orgs, total = await api_service.list_user_organizations(
            user_id="user-a",
            limit=20,
            offset=0,
        )

        assert total == 1
        assert orgs[0]["organization_id"] == "org-a"

    @pytest.mark.asyncio
    async def test_get_organization_enforces_membership_check(self, org_b):
        """get_organization enforces membership check."""
        mock_org_service = MagicMock()
        mock_org_service.get_organization = AsyncMock(return_value=org_b)
        mock_org_service.get_member_role = AsyncMock(return_value=None)

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.get_organization(
                organization_id="org-b",
                user_id="user-a",
            )

    @pytest.mark.asyncio
    async def test_list_members_enforces_membership_check(self, org_b):
        """list_organization_members enforces membership check."""
        mock_org_service = MagicMock()
        mock_org_service.get_member_role = AsyncMock(return_value=None)

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.list_organization_members(
                organization_id="org-b",
                user_id="user-a",
                limit=20,
                offset=0,
            )

    @pytest.mark.asyncio
    async def test_update_organization_enforces_membership_check(self, org_b):
        """update_organization enforces membership check."""
        mock_org_service = MagicMock()
        mock_org_service.get_organization = AsyncMock(return_value=org_b)
        mock_org_service.get_member_role = AsyncMock(return_value=None)

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.update_organization(
                organization_id="org-b",
                user_id="user-a",
                name="Hacked Name",
            )

    @pytest.mark.asyncio
    async def test_delete_organization_enforces_membership_check(self, org_b):
        """delete_organization enforces membership check."""
        mock_org_service = MagicMock()
        mock_org_service.get_organization = AsyncMock(return_value=org_b)
        mock_org_service.get_member_role = AsyncMock(return_value=None)

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.delete_organization(
                organization_id="org-b",
                user_id="user-a",
            )

    @pytest.mark.asyncio
    async def test_get_settings_enforces_membership_check(self, org_b):
        """get_organization_settings enforces membership check."""
        mock_org_service = MagicMock()
        mock_org_service.get_member_role = AsyncMock(return_value=None)

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.get_organization_settings(
                organization_id="org-b",
                user_id="user-a",
            )

    @pytest.mark.asyncio
    async def test_update_settings_enforces_membership_check(self, org_b):
        """update_organization_settings enforces membership check."""
        mock_org_service = MagicMock()
        mock_org_service.get_member_role = AsyncMock(return_value=None)

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.update_organization_settings(
                organization_id="org-b",
                user_id="user-a",
                settings={"allow_public_cases": True},
            )

    @pytest.mark.asyncio
    async def test_add_member_enforces_admin_check(self, org_b):
        """add_member enforces admin/owner check."""
        mock_org_service = MagicMock()
        mock_org_service.get_member_role = AsyncMock(return_value=ROLE_MEMBER)  # Not admin

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.add_member(
                organization_id="org-b",
                requesting_user_id="user-a",
                email="new@test.com",
                role="member",
            )

    @pytest.mark.asyncio
    async def test_remove_member_enforces_admin_check(self, org_b):
        """remove_member enforces admin/owner check."""
        mock_org_service = MagicMock()
        mock_org_service.get_member_role = AsyncMock(return_value=ROLE_MEMBER)  # Not admin

        api_service = APIOrganizationService(
            organization_service=mock_org_service,
        )

        with pytest.raises(AuthorizationError):
            await api_service.remove_member(
                organization_id="org-b",
                requesting_user_id="user-a",
                target_user_id="user-member",
            )


# ============================================================
# Cross-Organization Leak Prevention - API Endpoints
# ============================================================


class TestCrossOrganizationLeakPrevention:
    """Tests that API endpoints prevent cross-organization data leaks."""

    def test_user_a_cannot_access_org_b_via_api(self, client, user_a, org_b):
        """User A cannot access User B's organization via API."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization = AsyncMock(
                side_effect=AuthorizationError("Organization membership required")
            )
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-b",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403
            assert "membership" in response.json()["detail"].lower()

    def test_user_a_cannot_list_org_b_members(self, client, user_a):
        """User A cannot list org B members."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.list_organization_members = AsyncMock(
                side_effect=AuthorizationError("Organization membership required")
            )
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-b/members",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_user_a_cannot_update_org_b(self, client, user_a):
        """User A cannot update org B."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_organization = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-b",
                headers={"Authorization": "Bearer valid-token"},
                json={"name": "Hacked Name"}
            )

            assert response.status_code == 403

    def test_user_a_cannot_delete_org_b(self, client, user_a):
        """User A cannot delete org B."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.delete_organization = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/organizations/org-b",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_user_a_cannot_add_member_to_org_b(self, client, user_a):
        """User A cannot add member to org B."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.add_member = AsyncMock(
                side_effect=AuthorizationError("Organization admin access required")
            )
            mock_get_service.return_value = mock_service

            response = client.post(
                "/api/v1/organizations/org-b/members",
                headers={"Authorization": "Bearer valid-token"},
                json={"email": "attacker@test.com", "role": "admin"}
            )

            assert response.status_code == 403

    def test_user_a_cannot_remove_member_from_org_b(self, client, user_a):
        """User A cannot remove member from org B."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.remove_member = AsyncMock(
                side_effect=AuthorizationError("Organization admin access required")
            )
            mock_get_service.return_value = mock_service

            response = client.delete(
                "/api/v1/organizations/org-b/members/user-b",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_user_a_cannot_view_org_b_settings(self, client, user_a):
        """User A cannot view org B settings."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.get_organization_settings = AsyncMock(
                side_effect=AuthorizationError("Organization membership required")
            )
            mock_get_service.return_value = mock_service

            response = client.get(
                "/api/v1/organizations/org-b/settings",
                headers={"Authorization": "Bearer valid-token"}
            )

            assert response.status_code == 403

    def test_user_a_cannot_update_org_b_settings(self, client, user_a):
        """User A cannot update org B settings."""
        with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth, \
             patch("faultmaven.api.v1.routes.organizations.get_api_organization_service") as mock_get_service:

            mock_auth = MagicMock()
            mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(
                return_value=user_a
            )
            mock_get_auth.return_value = mock_auth

            mock_service = MagicMock()
            mock_service.update_organization_settings = AsyncMock(
                side_effect=AuthorizationError("Organization owner access required")
            )
            mock_get_service.return_value = mock_service

            response = client.patch(
                "/api/v1/organizations/org-b/settings",
                headers={"Authorization": "Bearer valid-token"},
                json={"allow_public_cases": True}
            )

            assert response.status_code == 403
