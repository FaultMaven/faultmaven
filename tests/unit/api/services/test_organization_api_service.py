"""Unit tests for APIOrganizationService (TASK-021)

Tests the API service layer for organization management.

Coverage Target: 30-40 tests
"""

from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.api.services.organization_api_service import (
    APIOrganizationService,
    ROLE_OWNER,
    ROLE_ADMIN,
    ROLE_MEMBER,
)
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.models.interfaces_user import (
    Organization,
    OrganizationMember,
    OrgPlanTier,
)


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_organization_service():
    """Create mock OrganizationService."""
    service = MagicMock()

    # Set up async methods
    service.create_organization = AsyncMock()
    service.get_organization = AsyncMock()
    service.get_organization_by_slug = AsyncMock()
    service.update_organization = AsyncMock()
    service.delete_organization = AsyncMock()
    service.list_user_organizations = AsyncMock()
    service.list_organization_members = AsyncMock()
    service.get_member_role = AsyncMock()
    service.add_member = AsyncMock()
    service.remove_member = AsyncMock()
    service.update_member_role = AsyncMock()
    service.user_has_permission = AsyncMock()

    return service


@pytest.fixture
def mock_user_service():
    """Create mock UserService."""
    service = MagicMock()
    service.get_user = AsyncMock()
    service.get_user_by_email = AsyncMock()
    return service


@pytest.fixture
def mock_auth_service():
    """Create mock AuthService."""
    service = MagicMock()
    service.revoke_user_tokens = AsyncMock(return_value=1)
    return service


@pytest.fixture
def api_service(mock_organization_service, mock_user_service, mock_auth_service):
    """Create APIOrganizationService with mocked dependencies."""
    return APIOrganizationService(
        organization_service=mock_organization_service,
        user_service=mock_user_service,
        auth_service=mock_auth_service,
    )


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


@pytest.fixture
def sample_members():
    """Create sample organization members."""
    now = datetime.now(timezone.utc)
    return [
        OrganizationMember(
            user_id="user-owner",
            org_id="org-123",
            role_id=ROLE_OWNER,
            joined_at=now,
        ),
        OrganizationMember(
            user_id="user-admin",
            org_id="org-123",
            role_id=ROLE_ADMIN,
            joined_at=now,
        ),
        OrganizationMember(
            user_id="user-member",
            org_id="org-123",
            role_id=ROLE_MEMBER,
            joined_at=now,
        ),
    ]


# ============================================================
# create_organization() Tests
# ============================================================


class TestCreateOrganization:
    """Tests for create_organization()."""

    @pytest.mark.asyncio
    async def test_creates_organization_successfully(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Creates organization successfully."""
        mock_organization_service.create_organization.return_value = sample_organization

        result = await api_service.create_organization(
            name="Test Organization",
            slug="test-org",
            creator_user_id="user-123",
            description="Test description",
            plan_tier="pro",
        )

        assert result.org_id == "org-123"
        assert result.name == "Test Organization"
        mock_organization_service.create_organization.assert_called_once()

    @pytest.mark.asyncio
    async def test_adds_creator_as_owner(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Creator becomes owner member."""
        mock_organization_service.create_organization.return_value = sample_organization

        await api_service.create_organization(
            name="Test Organization",
            slug="test-org",
            creator_user_id="user-123",
        )

        # Verify creator_user_id passed to domain service
        call_kwargs = mock_organization_service.create_organization.call_args[1]
        assert call_kwargs["creator_user_id"] == "user-123"

    @pytest.mark.asyncio
    async def test_validates_plan_tier_enum(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Validates plan tier enum values."""
        mock_organization_service.create_organization.return_value = sample_organization

        # Valid plan tiers
        for tier in ["free", "pro", "enterprise"]:
            await api_service.create_organization(
                name="Test",
                slug="test",
                creator_user_id="user-123",
                plan_tier=tier,
            )

    @pytest.mark.asyncio
    async def test_raises_validation_error_for_invalid_plan_tier(self, api_service):
        """Raises ValidationException on invalid plan tier."""
        with pytest.raises(ValidationException) as exc_info:
            await api_service.create_organization(
                name="Test",
                slug="test",
                creator_user_id="user-123",
                plan_tier="invalid",
            )

        assert "Invalid plan tier" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validates_slug_format_lowercase_hyphens(self, api_service):
        """Validates slug format (lowercase, hyphens only)."""
        with pytest.raises(ValidationException) as exc_info:
            await api_service.create_organization(
                name="Test",
                slug="Invalid_Slug!",
                creator_user_id="user-123",
            )

        assert "lowercase" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_raises_validation_error_on_duplicate_slug(
        self, api_service, mock_organization_service
    ):
        """Raises ValidationException on duplicate slug."""
        mock_organization_service.create_organization.side_effect = ValidationException(
            "Slug already exists"
        )

        with pytest.raises(ValidationException) as exc_info:
            await api_service.create_organization(
                name="Test",
                slug="existing-slug",
                creator_user_id="user-123",
            )

        assert "already exists" in str(exc_info.value).lower()


# ============================================================
# list_user_organizations() Tests
# ============================================================


class TestListUserOrganizations:
    """Tests for list_user_organizations()."""

    @pytest.mark.asyncio
    async def test_returns_organizations_user_is_member_of(
        self,
        api_service,
        mock_organization_service,
        sample_organization,
        sample_members,
    ):
        """Returns organizations user belongs to."""
        mock_organization_service.list_user_organizations.return_value = [
            sample_organization
        ]
        mock_organization_service.get_member_role.return_value = ROLE_OWNER
        mock_organization_service.list_organization_members.return_value = (
            sample_members
        )

        orgs, total = await api_service.list_user_organizations(
            user_id="user-owner",
            limit=20,
            offset=0,
        )

        assert total == 1
        assert len(orgs) == 1
        assert orgs[0]["organization_id"] == "org-123"

    @pytest.mark.asyncio
    async def test_pagination_works(
        self,
        api_service,
        mock_organization_service,
        sample_organization,
        sample_members,
    ):
        """Pagination works (limit, offset)."""
        orgs = [sample_organization] * 5
        mock_organization_service.list_user_organizations.return_value = orgs
        mock_organization_service.get_member_role.return_value = ROLE_MEMBER
        mock_organization_service.list_organization_members.return_value = (
            sample_members
        )

        result, total = await api_service.list_user_organizations(
            user_id="user-123",
            limit=2,
            offset=1,
        )

        assert total == 5
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_list_if_no_memberships(
        self, api_service, mock_organization_service
    ):
        """Returns empty list if no memberships."""
        mock_organization_service.list_user_organizations.return_value = []

        orgs, total = await api_service.list_user_organizations(
            user_id="user-123",
            limit=20,
            offset=0,
        )

        assert total == 0
        assert len(orgs) == 0


# ============================================================
# get_organization() Tests
# ============================================================


class TestGetOrganization:
    """Tests for get_organization()."""

    @pytest.mark.asyncio
    async def test_returns_organization_if_user_is_member(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Returns organization if user is member."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = ROLE_MEMBER

        result = await api_service.get_organization(
            organization_id="org-123",
            user_id="user-member",
        )

        assert result.org_id == "org-123"

    @pytest.mark.asyncio
    async def test_raises_authorization_error_if_not_member(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Raises AuthorizationError if not a member."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = None

        with pytest.raises(AuthorizationError) as exc_info:
            await api_service.get_organization(
                organization_id="org-123",
                user_id="non-member",
            )

        assert "membership required" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_raises_not_found_error_if_organization_doesnt_exist(
        self, api_service, mock_organization_service
    ):
        """Raises NotFoundError if organization doesn't exist."""
        mock_organization_service.get_organization.return_value = None

        with pytest.raises(NotFoundError):
            await api_service.get_organization(
                organization_id="nonexistent",
                user_id="user-123",
            )


# ============================================================
# update_organization() Tests
# ============================================================


class TestUpdateOrganization:
    """Tests for update_organization()."""

    @pytest.mark.asyncio
    async def test_owner_can_update_name_and_description(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Owner can update name and description."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = ROLE_OWNER
        mock_organization_service.update_organization.return_value = True

        result = await api_service.update_organization(
            organization_id="org-123",
            user_id="user-owner",
            name="Updated Name",
            description="Updated description",
        )

        assert result is not None
        mock_organization_service.update_organization.assert_called_once()

    @pytest.mark.asyncio
    async def test_admin_cannot_update_raises_authorization_error(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Admin CANNOT update (raises AuthorizationError)."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = ROLE_ADMIN

        with pytest.raises(AuthorizationError):
            await api_service.update_organization(
                organization_id="org-123",
                user_id="user-admin",
                name="Updated Name",
            )

    @pytest.mark.asyncio
    async def test_member_cannot_update_raises_authorization_error(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Member CANNOT update (raises AuthorizationError)."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = ROLE_MEMBER

        with pytest.raises(AuthorizationError):
            await api_service.update_organization(
                organization_id="org-123",
                user_id="user-member",
                name="Updated Name",
            )

    @pytest.mark.asyncio
    async def test_raises_not_found_error_if_organization_doesnt_exist(
        self, api_service, mock_organization_service
    ):
        """Raises NotFoundError if organization doesn't exist."""
        mock_organization_service.get_organization.return_value = None

        with pytest.raises(NotFoundError):
            await api_service.update_organization(
                organization_id="nonexistent",
                user_id="user-123",
                name="Updated Name",
            )


# ============================================================
# delete_organization() Tests
# ============================================================


class TestDeleteOrganization:
    """Tests for delete_organization()."""

    @pytest.mark.asyncio
    async def test_owner_can_soft_delete_organization(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Owner can soft-delete organization."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = ROLE_OWNER
        mock_organization_service.delete_organization.return_value = True

        result = await api_service.delete_organization(
            organization_id="org-123",
            user_id="user-owner",
        )

        assert result is True
        mock_organization_service.delete_organization.assert_called_once()

    @pytest.mark.asyncio
    async def test_admin_cannot_delete_raises_authorization_error(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Admin CANNOT delete (raises AuthorizationError)."""
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.get_member_role.return_value = ROLE_ADMIN

        with pytest.raises(AuthorizationError):
            await api_service.delete_organization(
                organization_id="org-123",
                user_id="user-admin",
            )

    @pytest.mark.asyncio
    async def test_raises_not_found_error_if_organization_doesnt_exist(
        self, api_service, mock_organization_service
    ):
        """Raises NotFoundError if organization doesn't exist."""
        mock_organization_service.get_organization.return_value = None

        with pytest.raises(NotFoundError):
            await api_service.delete_organization(
                organization_id="nonexistent",
                user_id="user-123",
            )


# ============================================================
# add_member() Tests
# ============================================================


class TestAddMember:
    """Tests for add_member()."""

    @pytest.mark.asyncio
    async def test_owner_can_add_members_with_any_role(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        sample_organization,
    ):
        """Owner can add members with any role."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # _require_org_admin check
            ROLE_OWNER,  # Get requester role for permission check
            None,  # Target user not already a member
        ]
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.list_organization_members.return_value = []
        mock_organization_service.add_member.return_value = True

        mock_user = MagicMock()
        mock_user.user_id = "user-new"
        mock_user.email = "new@test.com"
        mock_user.display_name = "New User"
        mock_user_service.get_user_by_email.return_value = mock_user

        result = await api_service.add_member(
            organization_id="org-123",
            requesting_user_id="user-owner",
            email="new@test.com",
            role="admin",
        )

        assert result["user_id"] == "user-new"
        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_admin_can_add_members_not_admins(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        sample_organization,
    ):
        """Admin can add members (not admin role)."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_ADMIN,  # _require_org_admin check
            ROLE_ADMIN,  # Get requester role for permission check
            None,  # Target not already a member
        ]
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.list_organization_members.return_value = []
        mock_organization_service.add_member.return_value = True

        mock_user = MagicMock()
        mock_user.user_id = "user-new"
        mock_user.email = "new@test.com"
        mock_user.display_name = "New User"
        mock_user_service.get_user_by_email.return_value = mock_user

        result = await api_service.add_member(
            organization_id="org-123",
            requesting_user_id="user-admin",
            email="new@test.com",
            role="member",
        )

        assert result["role"] == "member"

    @pytest.mark.asyncio
    async def test_admin_cannot_add_admin_role_raises_authorization_error(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        sample_organization,
    ):
        """Admin cannot add admin role (raises AuthorizationError)."""
        mock_organization_service.get_member_role.return_value = ROLE_ADMIN

        with pytest.raises(AuthorizationError) as exc_info:
            await api_service.add_member(
                organization_id="org-123",
                requesting_user_id="user-admin",
                email="new@test.com",
                role="admin",
            )

        assert "only owners" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_member_cannot_add_members_raises_authorization_error(
        self, api_service, mock_organization_service
    ):
        """Member CANNOT add members (raises AuthorizationError)."""
        mock_organization_service.get_member_role.return_value = ROLE_MEMBER

        with pytest.raises(AuthorizationError):
            await api_service.add_member(
                organization_id="org-123",
                requesting_user_id="user-member",
                email="new@test.com",
                role="member",
            )

    @pytest.mark.asyncio
    async def test_checks_max_members_limit_raises_conflict_error(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        sample_organization,
        sample_members,
    ):
        """Checks max_members limit (raises ConflictError)."""
        # Set max_members to current count
        sample_organization.max_members = len(sample_members)

        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # _require_org_admin check
            ROLE_OWNER,  # Get requester role for permission check
            None,  # Target not already a member
        ]
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.list_organization_members.return_value = (
            sample_members
        )

        mock_user = MagicMock()
        mock_user.user_id = "user-new"
        mock_user.email = "new@test.com"
        mock_user.display_name = "New User"
        mock_user_service.get_user_by_email.return_value = mock_user

        with pytest.raises(ConflictError) as exc_info:
            await api_service.add_member(
                organization_id="org-123",
                requesting_user_id="user-owner",
                email="new@test.com",
                role="member",
            )

        assert "max" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_user_must_exist_raises_not_found_error(
        self, api_service, mock_organization_service, mock_user_service
    ):
        """User must exist (raises NotFoundError)."""
        mock_organization_service.get_member_role.return_value = ROLE_OWNER
        mock_user_service.get_user_by_email.return_value = None

        with pytest.raises(NotFoundError):
            await api_service.add_member(
                organization_id="org-123",
                requesting_user_id="user-owner",
                email="nonexistent@test.com",
                role="member",
            )

    @pytest.mark.asyncio
    async def test_user_cannot_already_be_member_raises_conflict_error(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        sample_organization,
    ):
        """User cannot already be member (raises ConflictError)."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # _require_org_admin check
            ROLE_OWNER,  # Get requester role for permission check
            ROLE_MEMBER,  # Target already a member
        ]

        mock_user = MagicMock()
        mock_user.user_id = "user-existing"
        mock_user.email = "existing@test.com"
        mock_user_service.get_user_by_email.return_value = mock_user

        with pytest.raises(ConflictError) as exc_info:
            await api_service.add_member(
                organization_id="org-123",
                requesting_user_id="user-owner",
                email="existing@test.com",
                role="member",
            )

        assert "already" in str(exc_info.value).lower()


# ============================================================
# remove_member() Tests
# ============================================================


class TestRemoveMember:
    """Tests for remove_member()."""

    @pytest.mark.asyncio
    async def test_owner_can_remove_any_member(
        self, api_service, mock_organization_service, mock_auth_service
    ):
        """Owner can remove any member (except self)."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # _require_org_admin check
            ROLE_MEMBER,  # Get target role
            ROLE_OWNER,  # Get requester role
        ]
        mock_organization_service.remove_member.return_value = True

        result = await api_service.remove_member(
            organization_id="org-123",
            requesting_user_id="user-owner",
            target_user_id="user-member",
        )

        assert result is True
        mock_organization_service.remove_member.assert_called_once()

    @pytest.mark.asyncio
    async def test_admin_can_remove_members_not_owner_not_other_admins(
        self, api_service, mock_organization_service, mock_auth_service
    ):
        """Admin can remove members (not owner, not other admins)."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_ADMIN,  # _require_org_admin check
            ROLE_MEMBER,  # Get target role
            ROLE_ADMIN,  # Get requester role
        ]
        mock_organization_service.remove_member.return_value = True

        result = await api_service.remove_member(
            organization_id="org-123",
            requesting_user_id="user-admin",
            target_user_id="user-member",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_admin_cannot_remove_other_admins_raises_authorization_error(
        self, api_service, mock_organization_service
    ):
        """Admin cannot remove other admins (raises AuthorizationError)."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_ADMIN,  # _require_org_admin check
            ROLE_ADMIN,  # Get target role
            ROLE_ADMIN,  # Get requester role
        ]

        with pytest.raises(AuthorizationError):
            await api_service.remove_member(
                organization_id="org-123",
                requesting_user_id="user-admin1",
                target_user_id="user-admin2",
            )

    @pytest.mark.asyncio
    async def test_member_cannot_remove_anyone(
        self, api_service, mock_organization_service
    ):
        """Member CANNOT remove anyone."""
        mock_organization_service.get_member_role.return_value = ROLE_MEMBER

        with pytest.raises(AuthorizationError):
            await api_service.remove_member(
                organization_id="org-123",
                requesting_user_id="user-member",
                target_user_id="user-other",
            )

    @pytest.mark.asyncio
    async def test_cannot_remove_owner_raises_authorization_error(
        self, api_service, mock_organization_service
    ):
        """Cannot remove owner (raises AuthorizationError)."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # Requester is owner
            ROLE_OWNER,  # Target is also owner
        ]

        with pytest.raises(AuthorizationError) as exc_info:
            await api_service.remove_member(
                organization_id="org-123",
                requesting_user_id="user-owner",
                target_user_id="user-owner2",
            )

        assert "owner" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_raises_not_found_error_if_user_not_member(
        self, api_service, mock_organization_service
    ):
        """Raises NotFoundError if user not a member."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # Requester is owner
            None,  # Target not a member
        ]

        with pytest.raises(NotFoundError):
            await api_service.remove_member(
                organization_id="org-123",
                requesting_user_id="user-owner",
                target_user_id="nonexistent",
            )


# ============================================================
# update_member_role() Tests
# ============================================================


class TestUpdateMemberRole:
    """Tests for update_member_role()."""

    @pytest.mark.asyncio
    async def test_owner_can_update_any_members_role(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        mock_auth_service,
        sample_members,
    ):
        """Owner can update any member's role."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # Requester is owner
            ROLE_MEMBER,  # Target is member
        ]
        mock_organization_service.update_member_role.return_value = True
        mock_organization_service.list_organization_members.return_value = (
            sample_members
        )

        mock_user = MagicMock()
        mock_user.user_id = "user-member"
        mock_user.email = "member@test.com"
        mock_user.display_name = "Member User"
        mock_user_service.get_user.return_value = mock_user

        result = await api_service.update_member_role(
            organization_id="org-123",
            requesting_user_id="user-owner",
            target_user_id="user-member",
            role="admin",
        )

        assert result["role"] == "admin"

    @pytest.mark.asyncio
    async def test_admin_cannot_update_roles_raises_authorization_error(
        self, api_service, mock_organization_service
    ):
        """Admin CANNOT update roles (raises AuthorizationError)."""
        mock_organization_service.get_member_role.return_value = ROLE_ADMIN

        with pytest.raises(AuthorizationError):
            await api_service.update_member_role(
                organization_id="org-123",
                requesting_user_id="user-admin",
                target_user_id="user-member",
                role="admin",
            )

    @pytest.mark.asyncio
    async def test_cannot_set_role_to_owner_raises_validation_error(
        self, api_service, mock_organization_service
    ):
        """Cannot set role to 'owner' (use transfer ownership)."""
        mock_organization_service.get_member_role.return_value = ROLE_OWNER

        with pytest.raises(ValidationException) as exc_info:
            await api_service.update_member_role(
                organization_id="org-123",
                requesting_user_id="user-owner",
                target_user_id="user-member",
                role="owner",
            )

        assert "owner" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_revokes_jwt_tokens_on_role_change(
        self,
        api_service,
        mock_organization_service,
        mock_user_service,
        mock_auth_service,
        sample_members,
    ):
        """Revokes JWT tokens on role change."""
        mock_organization_service.get_member_role.side_effect = [
            ROLE_OWNER,  # Requester is owner
            ROLE_MEMBER,  # Target is member
        ]
        mock_organization_service.update_member_role.return_value = True
        mock_organization_service.list_organization_members.return_value = (
            sample_members
        )

        mock_user = MagicMock()
        mock_user.user_id = "user-member"
        mock_user.email = "member@test.com"
        mock_user.display_name = "Member User"
        mock_user_service.get_user.return_value = mock_user

        await api_service.update_member_role(
            organization_id="org-123",
            requesting_user_id="user-owner",
            target_user_id="user-member",
            role="admin",
        )

        mock_auth_service.revoke_user_tokens.assert_called_once_with("user-member")


# ============================================================
# Organization Settings Tests
# ============================================================


class TestGetOrganizationSettings:
    """Tests for get_organization_settings()."""

    @pytest.mark.asyncio
    async def test_member_can_view_settings(
        self,
        api_service,
        mock_organization_service,
        sample_organization,
        sample_members,
    ):
        """Member can view settings."""
        mock_organization_service.get_member_role.return_value = ROLE_MEMBER
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.list_organization_members.return_value = (
            sample_members
        )

        result = await api_service.get_organization_settings(
            organization_id="org-123",
            user_id="user-member",
        )

        assert result["organization_id"] == "org-123"
        assert "features" in result
        assert "settings" in result

    @pytest.mark.asyncio
    async def test_non_member_cannot_view_settings_raises_authorization_error(
        self, api_service, mock_organization_service
    ):
        """Non-member cannot view settings (raises AuthorizationError)."""
        mock_organization_service.get_member_role.return_value = None

        with pytest.raises(AuthorizationError):
            await api_service.get_organization_settings(
                organization_id="org-123",
                user_id="non-member",
            )


class TestUpdateOrganizationSettings:
    """Tests for update_organization_settings()."""

    @pytest.mark.asyncio
    async def test_owner_can_update_settings(
        self, api_service, mock_organization_service, sample_organization
    ):
        """Owner can update settings."""
        mock_organization_service.get_member_role.return_value = ROLE_OWNER
        mock_organization_service.get_organization.return_value = sample_organization
        mock_organization_service.update_organization.return_value = True

        result = await api_service.update_organization_settings(
            organization_id="org-123",
            user_id="user-owner",
            settings={"allow_public_cases": True},
        )

        assert result["organization_id"] == "org-123"

    @pytest.mark.asyncio
    async def test_admin_cannot_update_settings_raises_authorization_error(
        self, api_service, mock_organization_service
    ):
        """Admin cannot update settings (raises AuthorizationError)."""
        mock_organization_service.get_member_role.return_value = ROLE_ADMIN

        with pytest.raises(AuthorizationError):
            await api_service.update_organization_settings(
                organization_id="org-123",
                user_id="user-admin",
                settings={"allow_public_cases": True},
            )

    @pytest.mark.asyncio
    async def test_validates_session_timeout_range(
        self, api_service, mock_organization_service
    ):
        """Validates session_timeout_minutes range (15-480)."""
        mock_organization_service.get_member_role.return_value = ROLE_OWNER

        with pytest.raises(ValidationException):
            await api_service.update_organization_settings(
                organization_id="org-123",
                user_id="user-owner",
                settings={"session_timeout_minutes": 10},  # Too low
            )

    @pytest.mark.asyncio
    async def test_validates_default_case_priority(
        self, api_service, mock_organization_service
    ):
        """Validates default_case_priority values."""
        mock_organization_service.get_member_role.return_value = ROLE_OWNER

        with pytest.raises(ValidationException):
            await api_service.update_organization_settings(
                organization_id="org-123",
                user_id="user-owner",
                settings={"default_case_priority": "invalid"},
            )
