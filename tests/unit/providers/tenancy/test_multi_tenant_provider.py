"""Unit tests for MultiTenantProvider (TASK-023).

Test Coverage: 15-18 tests

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from faultmaven.exceptions import AuthorizationError, NotFoundError, ValidationException
from faultmaven.models.interfaces_user import Organization, OrgPlanTier
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider


@pytest.fixture
def mock_organization_repository():
    """Mock organization repository."""
    return AsyncMock()


@pytest.fixture
def multi_tenant_provider(mock_organization_repository):
    """MultiTenantProvider instance with mocked repository."""
    return MultiTenantProvider(organization_repository=mock_organization_repository)


@pytest.fixture
def test_organization():
    """Test organization fixture."""
    return Organization(
        organization_id="org_123",
        slug="acme-corp",
        name="Acme Corporation",
        description="Test organization",
        plan_tier=OrgPlanTier.ENTERPRISE,
        max_members=50,
        max_cases=1000,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return User(
        user_id="user_123",
        email="test@example.com",
        hashed_password="hashed",
        full_name="Test User",
    )


# ============================================================================
# Test: get_current_organization() validates membership
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_organization_validates_membership(
    multi_tenant_provider, mock_organization_repository, test_organization, mock_user
):
    """Test get_current_organization validates user is a member."""
    mock_organization_repository.get_organization.return_value = test_organization
    mock_organization_repository.get_member_role.return_value = "member"

    result = await multi_tenant_provider.get_current_organization(
        current_user=mock_user, organization_id="org_123"
    )

    assert result == test_organization
    mock_organization_repository.get_member_role.assert_called_once_with(
        organization_id="org_123", user_id=mock_user.user_id
    )


# ============================================================================
# Test: get_current_organization() raises if organization_id not provided
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_organization_raises_if_org_id_not_provided(
    multi_tenant_provider, mock_user
):
    """Test get_current_organization raises ValidationException if no organization_id."""
    with pytest.raises(ValidationException) as exc_info:
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id=None
        )

    assert "organization_id is required" in str(exc_info.value)
    assert "multi-tenant mode" in str(exc_info.value)


# ============================================================================
# Test: get_current_organization() raises if organization_id is empty string
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_organization_raises_if_org_id_empty(
    multi_tenant_provider, mock_user
):
    """Test get_current_organization raises ValidationException if organization_id is empty."""
    with pytest.raises(ValidationException) as exc_info:
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id=""
        )

    assert "organization_id is required" in str(exc_info.value)


# ============================================================================
# Test: get_current_organization() raises if org not found
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_organization_raises_if_org_not_found(
    multi_tenant_provider, mock_organization_repository, mock_user
):
    """Test get_current_organization raises NotFoundError if organization doesn't exist."""
    mock_organization_repository.get_organization.return_value = None

    with pytest.raises(NotFoundError) as exc_info:
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id="nonexistent_org"
        )

    assert "Organization not found" in str(exc_info.value)


# ============================================================================
# Test: get_current_organization() raises if user not member
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_organization_raises_if_user_not_member(
    multi_tenant_provider, mock_organization_repository, test_organization, mock_user
):
    """Test get_current_organization raises AuthorizationError if user not a member."""
    mock_organization_repository.get_organization.return_value = test_organization
    # User is not a member (get_member_role returns None)
    mock_organization_repository.get_member_role.return_value = None

    with pytest.raises(AuthorizationError) as exc_info:
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id="org_123"
        )

    assert "not a member" in str(exc_info.value)


# ============================================================================
# Test: get_current_organization() succeeds if user is member
# ============================================================================


@pytest.mark.asyncio
async def test_get_current_organization_succeeds_if_user_is_member(
    multi_tenant_provider, mock_organization_repository, test_organization, mock_user
):
    """Test get_current_organization succeeds when user is a valid member."""
    mock_organization_repository.get_organization.return_value = test_organization
    mock_organization_repository.get_member_role.return_value = "admin"

    result = await multi_tenant_provider.get_current_organization(
        current_user=mock_user, organization_id="org_123"
    )

    assert result == test_organization
    assert result.organization_id == "org_123"


# ============================================================================
# Test: get_default_organization() raises NotImplementedError
# ============================================================================


@pytest.mark.asyncio
async def test_get_default_organization_raises_not_implemented(multi_tenant_provider):
    """Test get_default_organization raises NotImplementedError in multi-tenant mode."""
    with pytest.raises(NotImplementedError) as exc_info:
        await multi_tenant_provider.get_default_organization()

    assert "Multi-tenant mode does not have a default organization" in str(
        exc_info.value
    )


# ============================================================================
# Test: is_multi_tenant() returns True
# ============================================================================


@pytest.mark.asyncio
async def test_is_multi_tenant_returns_true(multi_tenant_provider):
    """Test is_multi_tenant returns True for multi-tenant mode."""
    result = await multi_tenant_provider.is_multi_tenant()
    assert result is True


# ============================================================================
# Test: membership check uses member repository
# ============================================================================


@pytest.mark.asyncio
async def test_membership_check_uses_member_repository(
    multi_tenant_provider, mock_organization_repository, test_organization, mock_user
):
    """Test membership validation uses organization repository's get_member_role method."""
    mock_organization_repository.get_organization.return_value = test_organization
    mock_organization_repository.get_member_role.return_value = "owner"

    await multi_tenant_provider.get_current_organization(
        current_user=mock_user, organization_id="org_123"
    )

    # Verify get_member_role was called with correct parameters
    mock_organization_repository.get_member_role.assert_called_once_with(
        organization_id="org_123", user_id="user_123"
    )


# ============================================================================
# Test: different users can access different organizations
# ============================================================================


@pytest.mark.asyncio
async def test_different_users_can_access_different_organizations(
    multi_tenant_provider, mock_organization_repository
):
    """Test multi-tenant isolation allows different users in different orgs."""
    org1 = Organization(
        organization_id="org_1",
        slug="org-1",
        name="Organization 1",
        plan_tier=OrgPlanTier.PRO,
        max_members=10,
        max_cases=100,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    org2 = Organization(
        organization_id="org_2",
        slug="org-2",
        name="Organization 2",
        plan_tier=OrgPlanTier.ENTERPRISE,
        max_members=50,
        max_cases=1000,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    user1 = User(
        user_id="user_1",
        email="user1@org1.com",
        hashed_password="hashed",
        full_name="User One",
    )
    user2 = User(
        user_id="user_2",
        email="user2@org2.com",
        hashed_password="hashed",
        full_name="User Two",
    )

    # Configure mock to return different orgs for different org_ids
    async def get_org_side_effect(organization_id):
        return org1 if organization_id == "org_1" else org2

    async def get_role_side_effect(organization_id, user_id):
        if organization_id == "org_1" and user_id == "user_1":
            return "member"
        elif organization_id == "org_2" and user_id == "user_2":
            return "member"
        return None

    mock_organization_repository.get_organization.side_effect = get_org_side_effect
    mock_organization_repository.get_member_role.side_effect = get_role_side_effect

    result1 = await multi_tenant_provider.get_current_organization(
        current_user=user1, organization_id="org_1"
    )

    result2 = await multi_tenant_provider.get_current_organization(
        current_user=user2, organization_id="org_2"
    )

    assert result1.organization_id == "org_1"
    assert result2.organization_id == "org_2"
    assert result1.organization_id != result2.organization_id


# ============================================================================
# Test: user cannot access organization they're not a member of
# ============================================================================


@pytest.mark.asyncio
async def test_user_cannot_access_org_not_member_of(
    multi_tenant_provider, mock_organization_repository, test_organization, mock_user
):
    """Test strict isolation prevents unauthorized organization access."""
    mock_organization_repository.get_organization.return_value = test_organization
    # User tries to access org_123 but is not a member
    mock_organization_repository.get_member_role.return_value = None

    with pytest.raises(AuthorizationError):
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id="org_123"
        )


# ============================================================================
# Test: user with different roles can access same organization
# ============================================================================


@pytest.mark.asyncio
async def test_users_with_different_roles_can_access_same_org(
    multi_tenant_provider, mock_organization_repository, test_organization
):
    """Test users with different roles in same org can both access it."""
    admin_user = User(
        user_id="admin_1",
        email="admin@example.com",
        hashed_password="hashed",
        full_name="Admin User",
    )
    member_user = User(
        user_id="member_1",
        email="member@example.com",
        hashed_password="hashed",
        full_name="Member User",
    )

    mock_organization_repository.get_organization.return_value = test_organization

    async def get_role_side_effect(organization_id, user_id):
        if user_id == "admin_1":
            return "admin"
        elif user_id == "member_1":
            return "member"
        return None

    mock_organization_repository.get_member_role.side_effect = get_role_side_effect

    admin_result = await multi_tenant_provider.get_current_organization(
        current_user=admin_user, organization_id="org_123"
    )

    member_result = await multi_tenant_provider.get_current_organization(
        current_user=member_user, organization_id="org_123"
    )

    assert admin_result.organization_id == "org_123"
    assert member_result.organization_id == "org_123"
    assert admin_result == member_result


# ============================================================================
# Test: error details include user and organization info
# ============================================================================


@pytest.mark.asyncio
async def test_authorization_error_includes_details(
    multi_tenant_provider, mock_organization_repository, test_organization, mock_user
):
    """Test AuthorizationError includes helpful information in the message."""
    mock_organization_repository.get_organization.return_value = test_organization
    mock_organization_repository.get_member_role.return_value = None

    with pytest.raises(AuthorizationError) as exc_info:
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id="org_123"
        )

    # Check exception message includes user and organization info
    error_message = str(exc_info.value)
    assert mock_user.email in error_message
    assert test_organization.name in error_message


# ============================================================================
# Test: validation error includes hint for resolution
# ============================================================================


@pytest.mark.asyncio
async def test_validation_error_includes_hint(multi_tenant_provider, mock_user):
    """Test ValidationException includes helpful hint when organization_id missing."""
    with pytest.raises(ValidationException) as exc_info:
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id=None
        )

    error_message = str(exc_info.value)
    # Hint should suggest ways to provide organization_id
    assert (
        "JWT" in error_message or "header" in error_message or "query" in error_message
    )


# ============================================================================
# Test: organization lookup happens before membership check
# ============================================================================


@pytest.mark.asyncio
async def test_organization_lookup_before_membership_check(
    multi_tenant_provider, mock_organization_repository, mock_user
):
    """Test organization existence is checked before membership validation."""
    # Organization doesn't exist
    mock_organization_repository.get_organization.return_value = None

    with pytest.raises(NotFoundError):
        await multi_tenant_provider.get_current_organization(
            current_user=mock_user, organization_id="nonexistent"
        )

    # get_member_role should NOT be called if org doesn't exist
    mock_organization_repository.get_member_role.assert_not_called()


# ============================================================================
# Test: concurrent access to different organizations
# ============================================================================


@pytest.mark.asyncio
async def test_concurrent_access_different_organizations(
    multi_tenant_provider, mock_organization_repository
):
    """Test provider handles concurrent access to different organizations correctly."""
    import asyncio

    org_a = Organization(
        organization_id="org_a",
        slug="org-a",
        name="Org A",
        plan_tier=OrgPlanTier.PRO,
        max_members=10,
        max_cases=100,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    org_b = Organization(
        organization_id="org_b",
        slug="org-b",
        name="Org B",
        plan_tier=OrgPlanTier.ENTERPRISE,
        max_members=50,
        max_cases=1000,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    user_a = User(
        user_id="user_a",
        email="usera@example.com",
        hashed_password="hashed",
        full_name="User A",
    )
    user_b = User(
        user_id="user_b",
        email="userb@example.com",
        hashed_password="hashed",
        full_name="User B",
    )

    async def get_org_side_effect(organization_id):
        await asyncio.sleep(0.01)  # Simulate async delay
        return org_a if organization_id == "org_a" else org_b

    async def get_role_side_effect(organization_id, user_id):
        await asyncio.sleep(0.01)  # Simulate async delay
        if (organization_id == "org_a" and user_id == "user_a") or (
            organization_id == "org_b" and user_id == "user_b"
        ):
            return "member"
        return None

    mock_organization_repository.get_organization.side_effect = get_org_side_effect
    mock_organization_repository.get_member_role.side_effect = get_role_side_effect

    # Concurrent access
    results = await asyncio.gather(
        multi_tenant_provider.get_current_organization(user_a, "org_a"),
        multi_tenant_provider.get_current_organization(user_b, "org_b"),
    )

    assert results[0].organization_id == "org_a"
    assert results[1].organization_id == "org_b"
