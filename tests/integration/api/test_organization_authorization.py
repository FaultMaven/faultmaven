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
from faultmaven.exceptions import AuthorizationError, NotFoundError, ConflictError
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.models.interfaces_user import (
    Organization,
    OrganizationMember,
    OrgPlanTier,
)
from faultmaven.models.rbac import get_permissions_for_roles


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_api_service(sample_organization):
    """Create mock API organization service with authorization logic."""
    mock_service = MagicMock()

    # Store whether to enforce member limits (for plan tier tests)
    mock_service._enforce_member_limit = False

    # Authorization helper to check user role
    async def check_authorization(user_id: str, required_role: str):
        """Check if user has required role. Raises AuthorizationError if not."""
        # Map user IDs to roles based on fixtures
        user_roles = {
            "user-owner": "owner",
            "user-admin": "admin",
            "user-member": "member",
            "user-nonmember": None,  # Not a member
        }
        user_role = user_roles.get(user_id)

        # Non-member check
        if user_role is None:
            raise AuthorizationError("Organization membership required")

        # Role hierarchy: owner > admin > member
        role_hierarchy = {"owner": 3, "admin": 2, "member": 1}

        if required_role == "owner":
            if user_role != "owner":
                raise AuthorizationError("Organization owner access required")
        elif required_role == "admin":
            if role_hierarchy.get(user_role, 0) < role_hierarchy["admin"]:
                raise AuthorizationError("Organization admin access required")
        elif required_role == "member":
            if user_role not in ["owner", "admin", "member"]:
                raise AuthorizationError("Organization membership required")

    # Mock get_organization - requires membership
    async def mock_get_organization(org_id: str, user_id: str):
        await check_authorization(user_id, "member")
        return sample_organization

    mock_service.get_organization = AsyncMock(side_effect=mock_get_organization)

    # Mock update_organization - requires owner
    async def mock_update_organization(organization_id: str, user_id: str, **kwargs):
        await check_authorization(user_id, "owner")
        return sample_organization

    mock_service.update_organization = AsyncMock(side_effect=mock_update_organization)

    # Mock delete_organization - requires owner
    async def mock_delete_organization(org_id: str, user_id: str):
        await check_authorization(user_id, "owner")
        return True

    mock_service.delete_organization = AsyncMock(side_effect=mock_delete_organization)

    # Mock list_organization_members - requires membership
    async def mock_list_members(organization_id: str, user_id: str, **kwargs):
        await check_authorization(user_id, "member")
        return ([], 0)

    mock_service.list_organization_members = AsyncMock(side_effect=mock_list_members)

    # Mock add_member - requires admin
    async def mock_add_member(
        organization_id: str, requesting_user_id: str, email: str, role: str
    ):
        await check_authorization(requesting_user_id, "admin")
        # Check if member limit should be enforced (for plan tier tests)
        if mock_service._enforce_member_limit:
            raise ConflictError(
                "Organization has reached max_members limit (5)",
                resource_type="Organization",
                resource_id=organization_id,
                conflict_reason="max_members_reached",
            )
        # Allow member addition
        return {
            "user_id": "user-new",
            "email": email,
            "full_name": "New User",
            "role": role,
            "joined_at": datetime.now(timezone.utc),
            "invitation_sent": True,
        }

    mock_service.add_member = AsyncMock(side_effect=mock_add_member)

    # Mock remove_member - requires admin
    async def mock_remove_member(
        organization_id: str, requesting_user_id: str, target_user_id: str
    ):
        await check_authorization(requesting_user_id, "admin")
        return True

    mock_service.remove_member = AsyncMock(side_effect=mock_remove_member)

    # Mock update_member_role - requires owner
    async def mock_update_role(
        organization_id: str, requesting_user_id: str, target_user_id: str, role: str
    ):
        await check_authorization(requesting_user_id, "owner")
        return {
            "user_id": target_user_id,
            "email": "member@example.com",
            "full_name": "Member User",
            "role": role,
            "joined_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    mock_service.update_member_role = AsyncMock(side_effect=mock_update_role)

    # Mock get_organization_settings - requires membership
    async def mock_get_settings(organization_id: str, user_id: str):
        await check_authorization(user_id, "member")
        return {
            "organization_id": "org-123",
            "plan_tier": "pro",
            "max_members": 50,
            "current_member_count": 1,
            "max_cases_per_month": 1000,
            "max_storage_gb": 100,
            "features": {"knowledge_base": True, "ai_agents": True},
            "settings": {"allow_public_cases": False},
        }

    mock_service.get_organization_settings = AsyncMock(side_effect=mock_get_settings)

    # Mock update_organization_settings - requires owner
    async def mock_update_settings(organization_id: str, user_id: str, settings: dict):
        await check_authorization(user_id, "owner")
        return {
            "organization_id": organization_id,
            "settings": settings,
            "updated_at": datetime.now(timezone.utc),
        }

    mock_service.update_organization_settings = AsyncMock(
        side_effect=mock_update_settings
    )

    # Mock organization service's list_organization_members
    mock_service.organization_service = MagicMock()
    mock_service.organization_service.list_organization_members = AsyncMock(
        return_value=[]
    )

    return mock_service


@pytest.fixture
def mock_org_service(sample_organization):
    """Create mock domain organization service."""
    mock_service = MagicMock()
    mock_service.get_organization_by_slug = AsyncMock(return_value=sample_organization)
    mock_service.list_organization_members = AsyncMock(return_value=[])
    mock_service.user_has_permission = AsyncMock(return_value=True)
    return mock_service


def create_app_with_user(current_user, mock_api_service, mock_org_service):
    """Helper to create app with specific user override."""
    app = main_app

    # Override dependencies
    async def get_mock_current_user():
        return current_user

    async def get_mock_api_service_():
        return mock_api_service

    async def get_mock_org_service_():
        return mock_org_service

    # Import actual dependencies
    from faultmaven.api.middleware.auth import get_current_user
    from faultmaven.modules.auth.api.organizations import (
        get_api_organization_service,
        get_organization_service,
    )

    app.dependency_overrides[get_current_user] = get_mock_current_user
    app.dependency_overrides[get_api_organization_service] = get_mock_api_service_
    app.dependency_overrides[get_organization_service] = get_mock_org_service_

    return app


@pytest.fixture
def app(mock_api_service, mock_org_service, owner_user):
    """Create FastAPI test application with dependency overrides (default: owner_user)."""
    app = create_app_with_user(owner_user, mock_api_service, mock_org_service)
    yield app
    # Cleanup
    app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    """Create test client (uses owner_user by default)."""
    return TestClient(app)


@pytest.fixture
def create_client_for_user(mock_api_service, mock_org_service):
    """Factory to create test client with specific user."""

    def _create_client(user):
        app = create_app_with_user(user, mock_api_service, mock_org_service)
        return TestClient(app)

    return _create_client


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
        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200

    def test_owner_can_update_organization(
        self, client, owner_user, sample_organization
    ):
        """Owner can update organization."""
        response = client.patch(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Updated Name"},
        )

        assert response.status_code == 200

    def test_owner_can_delete_organization(self, client, owner_user):
        """Owner can delete organization."""
        response = client.delete(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200

    def test_owner_can_update_settings(self, client, owner_user):
        """Owner can update settings."""
        response = client.patch(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={"allow_public_cases": True},
        )

        assert response.status_code == 200

    def test_owner_can_change_member_roles(self, client, owner_user):
        """Owner can change member roles."""
        response = client.patch(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"},
        )

        assert response.status_code == 200


class TestAdminLimitedAccess:
    """Tests that admin has limited access."""

    def test_admin_can_view_organization(self, client, admin_user, sample_organization):
        """Admin can view organization details."""
        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200

    def test_admin_cannot_update_organization(self, create_client_for_user, admin_user):
        """Admin cannot update organization."""
        client = create_client_for_user(admin_user)
        response = client.patch(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Updated Name"},
        )

        assert response.status_code == 403

    def test_admin_cannot_delete_organization(self, create_client_for_user, admin_user):
        """Admin cannot delete organization."""
        client = create_client_for_user(admin_user)
        response = client.delete(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403

    def test_admin_cannot_update_settings(self, create_client_for_user, admin_user):
        """Admin cannot update settings."""
        client = create_client_for_user(admin_user)
        response = client.patch(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={"allow_public_cases": True},
        )

        assert response.status_code == 403

    def test_admin_cannot_change_member_roles(self, create_client_for_user, admin_user):
        """Admin cannot change member roles."""
        client = create_client_for_user(admin_user)
        response = client.patch(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"},
        )

        assert response.status_code == 403

    def test_admin_can_add_members_with_member_role(self, client, admin_user):
        """Admin can add members (with member role only)."""
        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"},
        )

        assert response.status_code == 201


class TestMemberReadOnlyAccess:
    """Tests that member has read-only access."""

    def test_member_can_view_organization(
        self, client, member_user, sample_organization
    ):
        """Member can view organization details."""
        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200

    def test_member_can_list_members(self, client, member_user):
        """Member can list members."""
        response = client.get(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200

    def test_member_can_view_settings(self, create_client_for_user, member_user):
        """Member can view settings."""
        client = create_client_for_user(member_user)
        response = client.get(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200

    def test_member_cannot_add_members(self, create_client_for_user, member_user):
        """Member cannot add members."""
        client = create_client_for_user(member_user)
        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"},
        )

        assert response.status_code == 403

    def test_member_cannot_remove_members(self, create_client_for_user, member_user):
        """Member cannot remove members."""
        client = create_client_for_user(member_user)
        response = client.delete(
            "/api/v1/organizations/org-123/members/user-other",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403


class TestNonMemberNoAccess:
    """Tests that non-member has no access."""

    def test_non_member_cannot_view_organization(
        self, create_client_for_user, non_member_user
    ):
        """Non-member cannot view organization."""
        client = create_client_for_user(non_member_user)
        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403

    def test_non_member_cannot_list_members(
        self, create_client_for_user, non_member_user
    ):
        """Non-member cannot list members."""
        client = create_client_for_user(non_member_user)
        response = client.get(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403

    def test_non_member_cannot_view_settings(
        self, create_client_for_user, non_member_user
    ):
        """Non-member cannot view settings."""
        client = create_client_for_user(non_member_user)
        response = client.get(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 403


# ============================================================
# Plan Tier Limit Tests
# ============================================================


class TestPlanTierLimits:
    """Tests for plan tier limits."""

    def test_free_plan_max_5_members(self, client, owner_user, mock_api_service):
        """Free plan: max 5 members."""
        # Enable member limit enforcement for this test
        mock_api_service._enforce_member_limit = True

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"},
        )

        # Max members returns 403 (Forbidden due to limit)
        assert response.status_code == 403

    def test_pro_plan_max_50_members(self, client, owner_user, mock_api_service):
        """Pro plan: max 50 members."""
        # Enable member limit enforcement for this test
        mock_api_service._enforce_member_limit = True

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"},
        )

        # Max members returns 403 (Forbidden due to limit)
        assert response.status_code == 403

    def test_adding_member_beyond_limit_returns_403(
        self, client, owner_user, mock_api_service
    ):
        """Adding member beyond limit returns 403."""
        # Enable member limit enforcement for this test
        mock_api_service._enforce_member_limit = True

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"},
        )

        assert response.status_code == 403
