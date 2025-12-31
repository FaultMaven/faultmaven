"""Unit tests for SingleTenantProvider (TASK-023).

Test Coverage: 12-15 tests

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.models.interfaces_user import Organization, OrgPlanTier
from faultmaven.models.user import User
from faultmaven.exceptions import NotFoundError


@pytest.fixture
def mock_organization_repository():
    """Mock organization repository."""
    return AsyncMock()


@pytest.fixture
def single_tenant_provider(mock_organization_repository):
    """SingleTenantProvider instance with mocked repository."""
    return SingleTenantProvider(organization_repository=mock_organization_repository)


@pytest.fixture
def default_organization():
    """Default organization fixture."""
    return Organization(
        org_id=SingleTenantProvider.DEFAULT_ORG_ID,
        slug=SingleTenantProvider.DEFAULT_ORG_SLUG,
        name=SingleTenantProvider.DEFAULT_ORG_NAME,
        description="Default organization for local deployment",
        plan_tier=OrgPlanTier.PRO,
        max_members=100,
        max_cases=None,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return User(
        user_id="user_123",
        email="test@example.com",
        hashed_password="hashed",
        full_name="Test User"
    )


# ============================================================================
# Test: get_current_organization() returns default organization
# ============================================================================

@pytest.mark.asyncio
async def test_get_current_organization_returns_default_org(
    single_tenant_provider,
    mock_organization_repository,
    default_organization,
    mock_user
):
    """Test get_current_organization returns default organization."""
    mock_organization_repository.get_organization.return_value = default_organization

    result = await single_tenant_provider.get_current_organization(
        current_user=mock_user
    )

    assert result == default_organization
    assert result.org_id == SingleTenantProvider.DEFAULT_ORG_ID
    mock_organization_repository.get_organization.assert_called_once_with(
        SingleTenantProvider.DEFAULT_ORG_ID
    )


# ============================================================================
# Test: get_current_organization() ignores organization_id parameter
# ============================================================================

@pytest.mark.asyncio
async def test_get_current_organization_ignores_organization_id_parameter(
    single_tenant_provider,
    mock_organization_repository,
    default_organization,
    mock_user
):
    """Test get_current_organization ignores provided organization_id."""
    mock_organization_repository.get_organization.return_value = default_organization

    # Provide a different organization_id - should be ignored
    result = await single_tenant_provider.get_current_organization(
        current_user=mock_user,
        organization_id="other_org_id"
    )

    assert result.org_id == SingleTenantProvider.DEFAULT_ORG_ID
    # Should still call with DEFAULT_ORG_ID, not the provided org_id
    mock_organization_repository.get_organization.assert_called_once_with(
        SingleTenantProvider.DEFAULT_ORG_ID
    )


# ============================================================================
# Test: get_default_organization() returns cached organization
# ============================================================================

@pytest.mark.asyncio
async def test_get_default_organization_returns_cached_org(
    single_tenant_provider,
    mock_organization_repository,
    default_organization
):
    """Test get_default_organization uses cache on subsequent calls."""
    mock_organization_repository.get_organization.return_value = default_organization

    # First call loads from repository
    result1 = await single_tenant_provider.get_default_organization()

    # Second call should use cache
    result2 = await single_tenant_provider.get_default_organization()

    assert result1 == default_organization
    assert result2 == default_organization
    # Repository should only be called once (cache hit on second call)
    assert mock_organization_repository.get_organization.call_count == 1


# ============================================================================
# Test: get_default_organization() loads from DB if not cached
# ============================================================================

@pytest.mark.asyncio
async def test_get_default_organization_loads_from_db_if_not_cached(
    single_tenant_provider,
    mock_organization_repository,
    default_organization
):
    """Test get_default_organization loads from database when cache is empty."""
    mock_organization_repository.get_organization.return_value = default_organization

    # Cache should be empty initially
    assert single_tenant_provider._default_org is None

    result = await single_tenant_provider.get_default_organization()

    assert result == default_organization
    mock_organization_repository.get_organization.assert_called_once_with(
        SingleTenantProvider.DEFAULT_ORG_ID
    )
    # Cache should now be populated
    assert single_tenant_provider._default_org == default_organization


# ============================================================================
# Test: get_default_organization() raises if not found
# ============================================================================

@pytest.mark.asyncio
async def test_get_default_organization_raises_if_not_found(
    single_tenant_provider,
    mock_organization_repository
):
    """Test get_default_organization raises NotFoundError if org doesn't exist."""
    mock_organization_repository.get_organization.return_value = None

    with pytest.raises(NotFoundError) as exc_info:
        await single_tenant_provider.get_default_organization()

    assert exc_info.value.resource_type == "Organization"
    assert exc_info.value.resource_id == SingleTenantProvider.DEFAULT_ORG_ID


# ============================================================================
# Test: ensure_default_organization_exists() creates if missing
# ============================================================================

@pytest.mark.asyncio
async def test_ensure_default_organization_creates_if_missing(
    single_tenant_provider,
    mock_organization_repository
):
    """Test ensure_default_organization_exists creates org if it doesn't exist."""
    # Repository returns None (org doesn't exist)
    mock_organization_repository.get_organization.return_value = None

    # Mock create_organization to return a new org
    created_org = Organization(
        org_id=SingleTenantProvider.DEFAULT_ORG_ID,
        slug=SingleTenantProvider.DEFAULT_ORG_SLUG,
        name=SingleTenantProvider.DEFAULT_ORG_NAME,
        description="Default organization for local/community deployment",
        plan_tier=OrgPlanTier.PRO,
        max_members=100,
        max_cases=None,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    mock_organization_repository.create_organization.return_value = created_org

    result = await single_tenant_provider.ensure_default_organization_exists()

    assert result.org_id == SingleTenantProvider.DEFAULT_ORG_ID
    assert result.plan_tier == OrgPlanTier.PRO
    assert result.max_members == 100
    assert result.max_cases is None
    mock_organization_repository.create_organization.assert_called_once()


# ============================================================================
# Test: ensure_default_organization_exists() returns existing if present
# ============================================================================

@pytest.mark.asyncio
async def test_ensure_default_organization_returns_existing_if_present(
    single_tenant_provider,
    mock_organization_repository,
    default_organization
):
    """Test ensure_default_organization_exists returns existing org without creating."""
    mock_organization_repository.get_organization.return_value = default_organization

    result = await single_tenant_provider.ensure_default_organization_exists()

    assert result == default_organization
    # Should not call create since org exists
    mock_organization_repository.create_organization.assert_not_called()


# ============================================================================
# Test: ensure_default_organization_exists() is idempotent
# ============================================================================

@pytest.mark.asyncio
async def test_ensure_default_organization_is_idempotent(
    single_tenant_provider,
    mock_organization_repository,
    default_organization
):
    """Test ensure_default_organization_exists can be called multiple times safely."""
    mock_organization_repository.get_organization.return_value = default_organization

    # Call multiple times
    result1 = await single_tenant_provider.ensure_default_organization_exists()
    result2 = await single_tenant_provider.ensure_default_organization_exists()

    assert result1 == default_organization
    assert result2 == default_organization
    # Should not create on subsequent calls
    mock_organization_repository.create_organization.assert_not_called()


# ============================================================================
# Test: is_multi_tenant() returns False
# ============================================================================

@pytest.mark.asyncio
async def test_is_multi_tenant_returns_false(single_tenant_provider):
    """Test is_multi_tenant returns False for single-tenant mode."""
    result = await single_tenant_provider.is_multi_tenant()
    assert result is False


# ============================================================================
# Test: default organization has PRO plan tier
# ============================================================================

@pytest.mark.asyncio
async def test_default_org_has_pro_plan_tier(
    single_tenant_provider,
    mock_organization_repository
):
    """Test default organization is created with PRO tier for local mode."""
    mock_organization_repository.get_organization.return_value = None

    created_org = Organization(
        org_id=SingleTenantProvider.DEFAULT_ORG_ID,
        slug=SingleTenantProvider.DEFAULT_ORG_SLUG,
        name=SingleTenantProvider.DEFAULT_ORG_NAME,
        description="Default organization for local/community deployment",
        plan_tier=OrgPlanTier.PRO,
        max_members=100,
        max_cases=None,
        settings={},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    mock_organization_repository.create_organization.return_value = created_org

    result = await single_tenant_provider.ensure_default_organization_exists()

    assert result.plan_tier == OrgPlanTier.PRO


# ============================================================================
# Test: default organization has correct ID and slug
# ============================================================================

@pytest.mark.asyncio
async def test_default_org_has_correct_id_and_slug(single_tenant_provider):
    """Test default organization constants have expected values."""
    assert SingleTenantProvider.DEFAULT_ORG_ID == "00000000-0000-0000-0000-000000000001"
    assert SingleTenantProvider.DEFAULT_ORG_SLUG == "default"
    assert SingleTenantProvider.DEFAULT_ORG_NAME == "Default Organization"

    # Verify ID is a valid UUID
    try:
        UUID(SingleTenantProvider.DEFAULT_ORG_ID)
    except ValueError:
        pytest.fail("DEFAULT_ORG_ID is not a valid UUID")


# ============================================================================
# Test: cache is populated after ensure_default_organization_exists()
# ============================================================================

@pytest.mark.asyncio
async def test_cache_populated_after_ensure_default_organization_exists(
    single_tenant_provider,
    mock_organization_repository,
    default_organization
):
    """Test cache is updated after ensure_default_organization_exists."""
    mock_organization_repository.get_organization.return_value = default_organization

    # Cache empty initially
    assert single_tenant_provider._default_org is None

    await single_tenant_provider.ensure_default_organization_exists()

    # Cache should now be populated
    assert single_tenant_provider._default_org == default_organization


# ============================================================================
# Test: multiple users get same default organization
# ============================================================================

@pytest.mark.asyncio
async def test_multiple_users_get_same_default_organization(
    single_tenant_provider,
    mock_organization_repository,
    default_organization
):
    """Test all users receive the same default organization (single-tenant isolation)."""
    mock_organization_repository.get_organization.return_value = default_organization

    user1 = User(user_id="user_1", email="user1@example.com", hashed_password="hashed", full_name="User One")
    user2 = User(user_id="user_2", email="user2@example.com", hashed_password="hashed", full_name="User Two")

    org1 = await single_tenant_provider.get_current_organization(current_user=user1)
    org2 = await single_tenant_provider.get_current_organization(current_user=user2)

    assert org1.org_id == org2.org_id
    assert org1.org_id == SingleTenantProvider.DEFAULT_ORG_ID
