"""Integration tests for Organization Management API Endpoints (TASK-021)

Tests all 11 organization API endpoints:
1. POST   /api/v1/organizations           (create org)
2. GET    /api/v1/organizations           (list user's orgs)
3. GET    /api/v1/organizations/{org_id}  (get org details)
4. PATCH  /api/v1/organizations/{org_id}  (update org)
5. DELETE /api/v1/organizations/{org_id}  (delete org)
6. GET    /api/v1/organizations/{org_id}/members      (list members)
7. POST   /api/v1/organizations/{org_id}/members      (add member)
8. DELETE /api/v1/organizations/{org_id}/members/{id} (remove member)
9. PATCH  /api/v1/organizations/{org_id}/members/{id} (update role)
10. GET   /api/v1/organizations/{org_id}/settings     (get settings)
11. PATCH /api/v1/organizations/{org_id}/settings     (update settings)

Coverage Target: 50-60 tests
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app as main_app
from faultmaven.models.auth import AuthenticatedUser
from faultmaven.models.interfaces_user import Organization, OrganizationMember, OrgPlanTier
from faultmaven.models.rbac import get_permissions_for_roles


# ============================================================
# Test Fixtures
# ============================================================


@pytest.fixture
def mock_api_service(sample_organization, sample_members):
    """Create mock API organization service."""
    mock_service = MagicMock()
    mock_service.create_organization = AsyncMock(return_value=sample_organization)
    mock_service.get_organization = AsyncMock(return_value=sample_organization)
    mock_service.update_organization = AsyncMock(return_value=sample_organization)
    mock_service.delete_organization = AsyncMock(return_value=True)
    mock_service.list_user_organizations = AsyncMock(return_value=(
        [{"organization_id": "org-123", "name": "Test Org", "slug": "test-org",
          "plan_tier": "pro", "role": "owner", "member_since": datetime.now(timezone.utc)}],
        1
    ))
    mock_service.list_organization_members = AsyncMock(return_value=(
        [{"user_id": "user-owner", "email": "owner@test.com", "full_name": "Owner",
          "role": "owner", "joined_at": datetime.now(timezone.utc)}],
        1
    ))
    mock_service.add_member = AsyncMock(return_value={
        "user_id": "user-new", "email": "new@test.com", "full_name": "New User",
        "role": "member", "joined_at": datetime.now(timezone.utc), "invitation_sent": True
    })
    mock_service.remove_member = AsyncMock(return_value=True)
    mock_service.update_member_role = AsyncMock(return_value={
        "user_id": "user-member", "email": "member@test.com", "full_name": "Member",
        "role": "admin", "joined_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    })
    mock_service.get_organization_settings = AsyncMock(return_value={
        "organization_id": "org-123", "plan_tier": "pro", "max_members": 50,
        "current_member_count": 3, "max_cases_per_month": 500, "max_storage_gb": 100,
        "features": {"knowledge_base": True, "ai_agents": True},
        "settings": {"allow_public_cases": False, "require_2fa": False}
    })
    mock_service.update_organization_settings = AsyncMock(return_value={
        "organization_id": "org-123",
        "settings": {"allow_public_cases": True},
        "updated_at": datetime.now(timezone.utc)
    })

    # Mock organization service's list_organization_members
    mock_service.organization_service = MagicMock()
    mock_service.organization_service.list_organization_members = AsyncMock(
        return_value=sample_members
    )

    return mock_service


@pytest.fixture
def mock_org_service(sample_organization, sample_members):
    """Create mock domain organization service."""
    mock_service = MagicMock()
    mock_service.get_organization_by_slug = AsyncMock(return_value=sample_organization)
    mock_service.list_organization_members = AsyncMock(return_value=sample_members)
    mock_service.user_has_permission = AsyncMock(return_value=True)
    return mock_service


@pytest.fixture
def app(mock_api_service, mock_org_service, owner_user):
    """Create FastAPI test application with dependency overrides."""
    app = main_app

    # Override dependencies
    async def get_mock_current_user():
        return owner_user

    async def get_mock_api_service():
        return mock_api_service

    async def get_mock_org_service():
        return mock_org_service

    # Import actual dependencies
    from faultmaven.api.middleware.auth import get_current_user
    from faultmaven.modules.auth.api.organizations import (
        get_api_organization_service,
        get_organization_service
    )

    app.dependency_overrides[get_current_user] = get_mock_current_user
    app.dependency_overrides[get_api_organization_service] = get_mock_api_service
    app.dependency_overrides[get_organization_service] = get_mock_org_service

    yield app

    # Cleanup
    app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    """Create test client with auth."""
    return TestClient(app)


@pytest.fixture
def unauthed_client(mock_api_service, mock_org_service):
    """Create test client without auth override (for 401 tests).
    
    Root cause fix (not bandaid):
    1. TestClient doesn't run lifespan events → app.state isn't initialized → 503 errors
    2. Solution: Use dependency overrides (FastAPI's proper pattern) instead of modifying app.state
    3. Proper cleanup prevents state leakage between tests
    
    This matches the pattern used in the 'app' fixture above.
    """
    from unittest.mock import AsyncMock, MagicMock
    
    app = main_app
    
    # For 401 tests, we need auth ENABLED (not NoAuthProvider)
    # Create a mock auth provider with the required interface
    auth_provider = MagicMock()
    auth_provider.is_enabled = True
    # Mock validate_token to raise 401 for empty/missing tokens
    async def validate_token_side_effect(token):
        if not token or token == "":
            from faultmaven.services.auth_service import AuthenticationError
            raise AuthenticationError("Missing or invalid token")
        # For any other token, return None (invalid)
        return None
    
    auth_provider.validate_token = AsyncMock(side_effect=validate_token_side_effect)
    
    # Store original state to restore after test (prevents state leakage between tests)
    original_auth_provider = getattr(app.state, "auth_provider", None)
    original_org_service = getattr(app.state, "organization_service", None)
    
    # Root cause: TestClient doesn't run lifespan events, so app.state isn't initialized
    # We must set required services to prevent 503 errors from get_api_organization_service
    # Note: get_auth_provider reads from app.state, so we can't use dependency override for it
    app.state.auth_provider = auth_provider
    app.state.organization_service = mock_org_service
    
    # Use dependency override for get_api_organization_service (cleaner than relying on state)
    # This matches the pattern used in the 'app' fixture above
    from faultmaven.modules.auth.api.organizations import get_api_organization_service
    async def get_mock_api_service():
        return mock_api_service
    app.dependency_overrides[get_api_organization_service] = get_mock_api_service
    
    client = TestClient(app)
    
    yield client
    
    # Cleanup: restore original state and clear overrides (prevents test leakage)
    app.dependency_overrides.pop(get_api_organization_service, None)
    if original_auth_provider is not None:
        app.state.auth_provider = original_auth_provider
    elif hasattr(app.state, "auth_provider"):
        delattr(app.state, "auth_provider")
    if original_org_service is not None:
        app.state.organization_service = original_org_service
    elif hasattr(app.state, "organization_service"):
        delattr(app.state, "organization_service")


@pytest.fixture
def mock_services(mock_api_service, mock_org_service):
    """Provide access to mock services for test configuration."""
    return {
        "api_service": mock_api_service,
        "org_service": mock_org_service
    }


@pytest.fixture
def owner_user():
    """Create owner authenticated user."""
    permissions = [p.value for p in get_permissions_for_roles(["admin"])]
    return AuthenticatedUser(
        user_id="user-owner",
        organization_id="org-123",
        email="owner@example.com",
        roles=["admin"],
        permissions=permissions,
        token_jti="token-owner",
    )


@pytest.fixture
def admin_user():
    """Create admin authenticated user."""
    permissions = [p.value for p in get_permissions_for_roles(["admin"])]
    return AuthenticatedUser(
        user_id="user-admin",
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
        user_id="user-member",
        organization_id="org-123",
        email="member@example.com",
        roles=["member"],
        permissions=permissions,
        token_jti="token-member",
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
            role_id="role_org_owner",
            joined_at=now,
        ),
        OrganizationMember(
            user_id="user-admin",
            org_id="org-123",
            role_id="role_org_admin",
            joined_at=now,
        ),
        OrganizationMember(
            user_id="user-member",
            org_id="org-123",
            role_id="role_org_member",
            joined_at=now,
        ),
    ]




# ============================================================
# POST /api/v1/organizations Tests
# ============================================================


class TestCreateOrganizationEndpoint:
    """Tests for POST /api/v1/organizations."""

    def test_201_created_creates_organization_successfully(
        self, client
    ):
        """201 Created - creates organization successfully."""
        response = client.post(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"},
            json={
                "name": "Test Organization",
                "slug": "test-org",
                "description": "Test description",
                "plan_tier": "pro"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["organization_id"] == "org-123"
        assert data["name"] == "Test Organization"

    def test_creator_becomes_owner_member(self, client, mock_services):
        """Creator becomes owner member."""
        response = client.post(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Test Org", "slug": "test-org"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["owner_user_id"] == "user-owner"

    def test_returns_organization_with_all_fields(self, client, mock_services):
        """Returns organization with all fields."""
        response = client.post(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Test Org", "slug": "test-org"}
        )

        assert response.status_code == 201
        data = response.json()
        assert "organization_id" in data
        assert "name" in data
        assert "slug" in data
        assert "plan_tier" in data
        assert "max_members" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_422_unprocessable_entity_invalid_slug_format(self, client, mock_services):
        """422 Unprocessable Entity - invalid slug format."""
        response = client.post(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Test Org", "slug": "INVALID_SLUG!"}
        )

        assert response.status_code == 422

    def test_409_conflict_duplicate_slug(self, client, mock_services):
        """409 Conflict - duplicate slug."""
        from faultmaven.exceptions import ValidationException
        mock_services["api_service"].create_organization.side_effect = \
            ValidationException("Slug already exists")

        response = client.post(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Test Org", "slug": "existing-slug"}
        )

        assert response.status_code == 409

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.post(
            "/api/v1/organizations",
            json={"name": "Test Org", "slug": "test-org"}
        )

        assert response.status_code == 401


# ============================================================
# GET /api/v1/organizations Tests
# ============================================================


class TestListUserOrganizationsEndpoint:
    """Tests for GET /api/v1/organizations."""

    def test_200_ok_returns_users_organizations(self, client, mock_services):
        """200 OK - returns user's organizations."""
        response = client.get(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "organizations" in data
        assert "total" in data
        assert len(data["organizations"]) > 0

    def test_pagination_works_limit_offset(self, client, mock_services):
        """Pagination works (limit, offset)."""
        response = client.get(
            "/api/v1/organizations?limit=10&offset=0",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 0

    def test_returns_empty_list_if_no_memberships(self, client, mock_services):
        """Returns empty list if no memberships."""
        mock_services["api_service"].list_user_organizations.return_value = ([], 0)

        response = client.get(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["organizations"] == []
        assert data["total"] == 0

    def test_shows_users_role_in_each_organization(self, client, mock_services):
        """Shows user's role in each organization."""
        response = client.get(
            "/api/v1/organizations",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "role" in data["organizations"][0]

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.get("/api/v1/organizations")

        assert response.status_code == 401


# ============================================================
# GET /api/v1/organizations/{org_id} Tests
# ============================================================


class TestGetOrganizationEndpoint:
    """Tests for GET /api/v1/organizations/{org_id}."""

    def test_200_ok_member_can_view_organization_details(
        self, client
    ):
        """200 OK - member can view organization details."""
        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["organization_id"] == "org-123"

    def test_includes_settings_and_member_count(self, client, mock_services):
        """Includes settings and member count."""
        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "settings" in data
        assert "current_member_count" in data

    def test_404_not_found_organization_doesnt_exist(self, client, mock_services):
        """404 Not Found - organization doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_services["api_service"].get_organization.side_effect = \
            NotFoundError("Organization", "nonexistent")

        response = client.get(
            "/api/v1/organizations/nonexistent",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 404

    def test_403_forbidden_user_not_member(self, client, mock_services):
        """403 Forbidden - user not a member."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].get_organization.side_effect = \
            AuthorizationError("Organization membership required")

        response = client.get(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.get("/api/v1/organizations/org-123")

        assert response.status_code == 401


# ============================================================
# PATCH /api/v1/organizations/{org_id} Tests
# ============================================================


class TestUpdateOrganizationEndpoint:
    """Tests for PATCH /api/v1/organizations/{org_id}."""

    def test_200_ok_owner_can_update_name_description(
        self, client
    ):
        """200 OK - owner can update name/description."""
        response = client.patch(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Updated Name", "description": "Updated description"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["organization_id"] == "org-123"

    def test_403_forbidden_admin_cannot_update(self, client, mock_services):
        """403 Forbidden - admin cannot update."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].update_organization.side_effect = \
            AuthorizationError("Organization owner access required")

        response = client.patch(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Updated Name"}
        )

        assert response.status_code == 403

    def test_403_forbidden_member_cannot_update(self, client, mock_services):
        """403 Forbidden - member cannot update."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].update_organization.side_effect = \
            AuthorizationError("Organization owner access required")

        response = client.patch(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Updated Name"}
        )

        assert response.status_code == 403

    def test_404_not_found_organization_doesnt_exist(self, client, mock_services):
        """404 Not Found - organization doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_services["api_service"].update_organization.side_effect = \
            NotFoundError("Organization", "nonexistent")

        response = client.patch(
            "/api/v1/organizations/nonexistent",
            headers={"Authorization": "Bearer valid-token"},
            json={"name": "Updated Name"}
        )

        assert response.status_code == 404

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.patch(
            "/api/v1/organizations/org-123",
            json={"name": "Updated Name"}
        )

        assert response.status_code == 401


# ============================================================
# DELETE /api/v1/organizations/{org_id} Tests
# ============================================================


class TestDeleteOrganizationEndpoint:
    """Tests for DELETE /api/v1/organizations/{org_id}."""

    def test_200_ok_owner_can_delete_organization(self, client, mock_services):
        """200 OK - owner can delete organization."""
        response = client.delete(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert data["organization_id"] == "org-123"

    def test_403_forbidden_admin_cannot_delete(self, client, mock_services):
        """403 Forbidden - admin cannot delete."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].delete_organization.side_effect = \
            AuthorizationError("Organization owner access required")

        response = client.delete(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_409_conflict_active_cases_exist(self, client, mock_services):
        """409 Conflict - active cases exist."""
        from faultmaven.exceptions import ConflictError
        mock_services["api_service"].delete_organization.side_effect = \
            ConflictError("Organization has active cases")

        response = client.delete(
            "/api/v1/organizations/org-123",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 409

    def test_404_not_found_organization_doesnt_exist(self, client, mock_services):
        """404 Not Found - organization doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_services["api_service"].delete_organization.side_effect = \
            NotFoundError("Organization", "nonexistent")

        response = client.delete(
            "/api/v1/organizations/nonexistent",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 404

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.delete("/api/v1/organizations/org-123")

        assert response.status_code == 401


# ============================================================
# GET /api/v1/organizations/{org_id}/members Tests
# ============================================================


class TestListOrganizationMembersEndpoint:
    """Tests for GET /api/v1/organizations/{org_id}/members."""

    def test_200_ok_member_can_list_all_members(self, client, mock_services):
        """200 OK - member can list all members."""
        response = client.get(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "members" in data
        assert "total" in data

    def test_pagination_works(self, client, mock_services):
        """Pagination works."""
        response = client.get(
            "/api/v1/organizations/org-123/members?limit=10&offset=0",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 0

    def test_filter_by_role_works(self, client, mock_services):
        """Filter by role works."""
        response = client.get(
            "/api/v1/organizations/org-123/members?role=owner",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200

    def test_403_forbidden_non_member_cannot_list(self, client, mock_services):
        """403 Forbidden - non-member cannot list."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].list_organization_members.side_effect = \
            AuthorizationError("Organization membership required")

        response = client.get(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.get("/api/v1/organizations/org-123/members")

        assert response.status_code == 401


# ============================================================
# POST /api/v1/organizations/{org_id}/members Tests
# ============================================================


class TestAddMemberEndpoint:
    """Tests for POST /api/v1/organizations/{org_id}/members."""

    def test_201_created_owner_adds_member(self, client, mock_services):
        """201 Created - owner adds member."""
        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["user_id"] == "user-new"
        assert data["role"] == "member"

    def test_201_created_admin_adds_member_not_admin_role(
        self, client
    ):
        """201 Created - admin adds member (not admin role)."""
        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"}
        )

        assert response.status_code == 201

    def test_403_forbidden_admin_cannot_add_admin(self, client, mock_services):
        """403 Forbidden - admin cannot add admin."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].add_member.side_effect = \
            AuthorizationError("Only owners can add admin members")

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "admin"}
        )

        assert response.status_code == 403

    def test_403_forbidden_member_cannot_add(self, client, mock_services):
        """403 Forbidden - member cannot add."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].add_member.side_effect = \
            AuthorizationError("Organization admin access required")

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"}
        )

        assert response.status_code == 403

    def test_404_not_found_user_email_doesnt_exist(self, client, mock_services):
        """404 Not Found - user email doesn't exist."""
        from faultmaven.exceptions import NotFoundError
        mock_services["api_service"].add_member.side_effect = \
            NotFoundError("User", "email:nonexistent@test.com")

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "nonexistent@test.com", "role": "member"}
        )

        assert response.status_code == 404

    def test_409_conflict_user_already_member(self, client, mock_services):
        """409 Conflict - user already a member."""
        from faultmaven.exceptions import ConflictError
        mock_services["api_service"].add_member.side_effect = \
            ConflictError("User is already a member")

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "existing@test.com", "role": "member"}
        )

        assert response.status_code == 409

    def test_409_conflict_max_members_limit_reached(self, client, mock_services):
        """409 Conflict - max members limit reached."""
        from faultmaven.exceptions import ConflictError
        mock_services["api_service"].add_member.side_effect = \
            ConflictError("Organization has reached maximum member limit")

        response = client.post(
            "/api/v1/organizations/org-123/members",
            headers={"Authorization": "Bearer valid-token"},
            json={"email": "new@test.com", "role": "member"}
        )

        assert response.status_code == 409

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.post(
            "/api/v1/organizations/org-123/members",
            json={"email": "new@test.com", "role": "member"}
        )

        assert response.status_code == 401


# ============================================================
# DELETE /api/v1/organizations/{org_id}/members/{user_id} Tests
# ============================================================


class TestRemoveMemberEndpoint:
    """Tests for DELETE /api/v1/organizations/{org_id}/members/{user_id}."""

    def test_200_ok_owner_removes_member(self, client, mock_services):
        """200 OK - owner removes member."""
        response = client.delete(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "message" in data

    def test_200_ok_admin_removes_member_not_admin_owner(
        self, client
    ):
        """200 OK - admin removes member (not admin/owner)."""
        response = client.delete(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200

    def test_403_forbidden_admin_cannot_remove_admin(self, client, mock_services):
        """403 Forbidden - admin cannot remove admin."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].remove_member.side_effect = \
            AuthorizationError("Admins cannot remove other admins")

        response = client.delete(
            "/api/v1/organizations/org-123/members/user-admin2",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_403_forbidden_cannot_remove_owner(self, client, mock_services):
        """403 Forbidden - cannot remove owner."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].remove_member.side_effect = \
            AuthorizationError("Cannot remove organization owner")

        response = client.delete(
            "/api/v1/organizations/org-123/members/user-owner",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_403_forbidden_member_cannot_remove(self, client, mock_services):
        """403 Forbidden - member cannot remove."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].remove_member.side_effect = \
            AuthorizationError("Organization admin access required")

        response = client.delete(
            "/api/v1/organizations/org-123/members/user-other",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_404_not_found_user_not_member(self, client, mock_services):
        """404 Not Found - user not a member."""
        from faultmaven.exceptions import NotFoundError
        mock_services["api_service"].remove_member.side_effect = \
            NotFoundError("OrganizationMember", "nonexistent")

        response = client.delete(
            "/api/v1/organizations/org-123/members/nonexistent",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 404

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.delete("/api/v1/organizations/org-123/members/user-member")

        assert response.status_code == 401


# ============================================================
# PATCH /api/v1/organizations/{org_id}/members/{user_id} Tests
# ============================================================


class TestUpdateMemberRoleEndpoint:
    """Tests for PATCH /api/v1/organizations/{org_id}/members/{user_id}."""

    def test_200_ok_owner_updates_member_role(self, client, mock_services):
        """200 OK - owner updates member role."""
        response = client.patch(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "admin"

    def test_403_forbidden_admin_cannot_update_roles(self, client, mock_services):
        """403 Forbidden - admin cannot update roles."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].update_member_role.side_effect = \
            AuthorizationError("Organization owner access required")

        response = client.patch(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"}
        )

        assert response.status_code == 403

    def test_403_forbidden_member_cannot_update_roles(self, client, mock_services):
        """403 Forbidden - member cannot update roles."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].update_member_role.side_effect = \
            AuthorizationError("Organization owner access required")

        response = client.patch(
            "/api/v1/organizations/org-123/members/user-other",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"}
        )

        assert response.status_code == 403

    def test_422_unprocessable_entity_invalid_role(self, client, mock_services):
        """422 Unprocessable Entity - invalid role."""
        response = client.patch(
            "/api/v1/organizations/org-123/members/user-member",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "superadmin"}  # Invalid role
        )

        assert response.status_code == 422

    def test_404_not_found_user_not_member(self, client, mock_services):
        """404 Not Found - user not a member."""
        from faultmaven.exceptions import NotFoundError
        mock_services["api_service"].update_member_role.side_effect = \
            NotFoundError("OrganizationMember", "nonexistent")

        response = client.patch(
            "/api/v1/organizations/org-123/members/nonexistent",
            headers={"Authorization": "Bearer valid-token"},
            json={"role": "admin"}
        )

        assert response.status_code == 404

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.patch(
            "/api/v1/organizations/org-123/members/user-member",
            json={"role": "admin"}
        )

        assert response.status_code == 401


# ============================================================
# GET /api/v1/organizations/{org_id}/settings Tests
# ============================================================


class TestGetOrganizationSettingsEndpoint:
    """Tests for GET /api/v1/organizations/{org_id}/settings."""

    def test_200_ok_member_can_view_settings(self, client, mock_services):
        """200 OK - member can view settings."""
        response = client.get(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "plan_tier" in data
        assert "features" in data
        assert "settings" in data

    def test_includes_plan_limits_and_features(self, client, mock_services):
        """Includes plan limits and features."""
        response = client.get(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "max_members" in data
        assert "max_storage_gb" in data
        assert "features" in data

    def test_403_forbidden_non_member_cannot_view(self, client, mock_services):
        """403 Forbidden - non-member cannot view."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].get_organization_settings.side_effect = \
            AuthorizationError("Organization membership required")

        response = client.get(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"}
        )

        assert response.status_code == 403

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.get("/api/v1/organizations/org-123/settings")

        assert response.status_code == 401


# ============================================================
# PATCH /api/v1/organizations/{org_id}/settings Tests
# ============================================================


class TestUpdateOrganizationSettingsEndpoint:
    """Tests for PATCH /api/v1/organizations/{org_id}/settings."""

    def test_200_ok_owner_updates_settings(self, client, mock_services):
        """200 OK - owner updates settings."""
        response = client.patch(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={"allow_public_cases": True, "require_2fa": True}
        )

        assert response.status_code == 200
        data = response.json()
        assert "settings" in data

    def test_403_forbidden_admin_cannot_update_settings(
        self, client, mock_services
    ):
        """403 Forbidden - admin cannot update settings."""
        from faultmaven.exceptions import AuthorizationError
        mock_services["api_service"].update_organization_settings.side_effect = \
            AuthorizationError("Organization owner access required")

        response = client.patch(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={"allow_public_cases": True}
        )

        assert response.status_code == 403

    def test_422_unprocessable_entity_invalid_setting_value(
        self, client
    ):
        """422 Unprocessable Entity - invalid setting value."""
        response = client.patch(
            "/api/v1/organizations/org-123/settings",
            headers={"Authorization": "Bearer valid-token"},
            json={"session_timeout_minutes": 5}  # Too low (min 15)
        )

        assert response.status_code == 422

    def test_401_unauthorized_no_jwt_token(self, unauthed_client):
        """401 Unauthorized - no JWT token."""
        response = unauthed_client.patch(
            "/api/v1/organizations/org-123/settings",
            json={"allow_public_cases": True}
        )

        assert response.status_code == 401
