# TASK-021: Organization Management API Endpoints

## Task Metadata
- **Phase**: Week 7, Day 2-3 (Multi-Tenant Foundation)
- **Priority**: P0 (Multi-tenancy foundation)
- **Estimated Time**: 6-8 hours
- **Dependencies**: TASK-019 (Admin User Management), PR #21 (JWT Auth Integration), PR #22 (Config Cleanup)
- **Assignee**: Developer
- **Reports To**: Solutions Architect

## Objective

**Implement organization management API endpoints** to complete the multi-tenant foundation:

1. **Organization CRUD operations** (create, read, update, delete)
2. **Organization member management** (invite, remove, update roles)
3. **Organization settings** (plan tier, limits, features)
4. **Multi-tenant isolation** (ensure users only see their organization data)
5. **Organization listing** (admin-only for platform management)
6. **Comprehensive testing** (90%+ coverage)

---

## Context

### Current State

✅ **Already Implemented**:
- `OrganizationService` (domain service layer) - `/home/swhouse/product/faultmaven/faultmaven/services/domain/organization_service.py`
- User authentication with JWT (TASK-017)
- Admin user management (TASK-019)
- Multi-tenant data model with `organization_id` on all resources

❌ **Missing**:
- API routes for organization management
- API service layer for organizations
- Multi-tenant validation middleware
- Organization-scoped permission checks

### Evolution Path
```
TASK-017: JWT Authentication ✅
TASK-018: User Management Service ✅
TASK-019: Admin User Management ✅
TASK-020: Remove Legacy Auth ✅ (PR #21)
TASK-021: Organization Management API ← YOU ARE HERE
TASK-022: Team Management API (next)
```

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                   API Layer (FastAPI)                       │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  /api/v1/organizations (new)                         │  │
│  │  - POST   /                  (create org)            │  │
│  │  - GET    /                  (list user's orgs)      │  │
│  │  - GET    /{org_id}          (get org details)       │  │
│  │  - PATCH  /{org_id}          (update org)            │  │
│  │  - DELETE /{org_id}          (delete org - owner)    │  │
│  │  - GET    /{org_id}/members  (list members)          │  │
│  │  - POST   /{org_id}/members  (invite member)         │  │
│  │  - DELETE /{org_id}/members/{user_id} (remove)       │  │
│  │  - PATCH  /{org_id}/members/{user_id} (update role)  │  │
│  │  - GET    /{org_id}/settings (get settings)          │  │
│  │  - PATCH  /{org_id}/settings (update settings-owner) │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│                 API Service Layer (new)                     │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  APIOrganizationService                              │  │
│  │  - create_organization()                             │  │
│  │  - get_organization()                                │  │
│  │  - update_organization()                             │  │
│  │  - delete_organization()                             │  │
│  │  - list_user_organizations()                         │  │
│  │  - add_member()                                      │  │
│  │  - remove_member()                                   │  │
│  │  - update_member_role()                              │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│              Domain Service Layer (existing)                │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  OrganizationService                                 │  │
│  │  - create_organization()                             │  │
│  │  - get_organization()                                │  │
│  │  - add_member()                                      │  │
│  │  - remove_member()                                   │  │
│  │  - check_permissions()                               │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│          Repository Layer (IOrganizationRepository)         │
│  - get_organization()                                       │
│  - create_organization()                                    │
│  - update_organization()                                    │
│  - delete_organization()                                    │
│  - get_organization_members()                               │
└─────────────────────────────────────────────────────────────┘
```

---

## API Specification

### 1. Create Organization (POST /api/v1/organizations)

**Request**:
```json
{
  "name": "Acme Corp",
  "slug": "acme-corp",
  "description": "Enterprise troubleshooting team",
  "plan_tier": "pro"
}
```

**Response** (201 Created):
```json
{
  "organization_id": "org_a1b2c3d4e5f6g7h8i",
  "name": "Acme Corp",
  "slug": "acme-corp",
  "description": "Enterprise troubleshooting team",
  "plan_tier": "pro",
  "max_members": 50,
  "current_member_count": 1,
  "owner_user_id": "user_xyz123",
  "created_at": "2025-12-30T12:00:00Z",
  "updated_at": "2025-12-30T12:00:00Z"
}
```

**Authorization**: Any authenticated user (becomes owner)

**Validation**:
- Slug must be unique across all organizations
- Slug format: lowercase letters, numbers, hyphens only
- Name required (1-100 chars)
- Plan tier: "free", "pro", "enterprise" (default: "free")

---

### 2. List User's Organizations (GET /api/v1/organizations)

**Query Parameters**:
- `limit` (int, default: 20, max: 100) - Pagination limit
- `offset` (int, default: 0) - Pagination offset

**Response** (200 OK):
```json
{
  "organizations": [
    {
      "organization_id": "org_a1b2c3d4e5f6g7h8i",
      "name": "Acme Corp",
      "slug": "acme-corp",
      "plan_tier": "pro",
      "role": "owner",
      "member_since": "2025-12-30T12:00:00Z"
    },
    {
      "organization_id": "org_b2c3d4e5f6g7h8i9j",
      "name": "Beta Inc",
      "slug": "beta-inc",
      "plan_tier": "free",
      "role": "member",
      "member_since": "2025-12-28T10:00:00Z"
    }
  ],
  "total": 2,
  "limit": 20,
  "offset": 0
}
```

**Authorization**: Authenticated user (returns their organizations)

---

### 3. Get Organization Details (GET /api/v1/organizations/{org_id})

**Response** (200 OK):
```json
{
  "organization_id": "org_a1b2c3d4e5f6g7h8i",
  "name": "Acme Corp",
  "slug": "acme-corp",
  "description": "Enterprise troubleshooting team",
  "plan_tier": "pro",
  "max_members": 50,
  "current_member_count": 15,
  "owner_user_id": "user_xyz123",
  "created_at": "2025-12-30T12:00:00Z",
  "updated_at": "2025-12-30T15:30:00Z",
  "settings": {
    "allow_public_cases": false,
    "require_2fa": true,
    "session_timeout_minutes": 60
  }
}
```

**Authorization**: Organization member (any role)

**Errors**:
- 404 Not Found - Organization doesn't exist
- 403 Forbidden - User not a member of this organization

---

### 4. Update Organization (PATCH /api/v1/organizations/{org_id})

**Request**:
```json
{
  "name": "Acme Corporation",
  "description": "Updated description"
}
```

**Response** (200 OK):
```json
{
  "organization_id": "org_a1b2c3d4e5f6g7h8i",
  "name": "Acme Corporation",
  "slug": "acme-corp",
  "description": "Updated description",
  ...
}
```

**Authorization**: Organization owner only

**Updatable Fields**:
- `name` (owner only)
- `description` (owner only)

**Non-updatable**:
- `slug` (immutable - breaking change for URLs)
- `plan_tier` (admin-only via separate endpoint)
- `organization_id` (immutable)

---

### 5. Delete Organization (DELETE /api/v1/organizations/{org_id})

**Response** (200 OK):
```json
{
  "message": "Organization deleted successfully",
  "organization_id": "org_a1b2c3d4e5f6g7h8i"
}
```

**Authorization**: Organization owner only

**Business Rules**:
- Soft delete (set `deleted_at` timestamp)
- Cannot delete if active cases exist (return 409 Conflict)
- Must archive all data first
- All members automatically removed

---

### 6. List Organization Members (GET /api/v1/organizations/{org_id}/members)

**Query Parameters**:
- `role` (optional) - Filter by role: "owner", "admin", "member"
- `limit` (int, default: 20, max: 100)
- `offset` (int, default: 0)

**Response** (200 OK):
```json
{
  "members": [
    {
      "user_id": "user_xyz123",
      "email": "owner@acme.com",
      "full_name": "Alice Owner",
      "role": "owner",
      "joined_at": "2025-12-30T12:00:00Z"
    },
    {
      "user_id": "user_abc456",
      "email": "admin@acme.com",
      "full_name": "Bob Admin",
      "role": "admin",
      "joined_at": "2025-12-30T13:00:00Z"
    }
  ],
  "total": 15,
  "limit": 20,
  "offset": 0
}
```

**Authorization**: Organization member (any role)

---

### 7. Add Organization Member (POST /api/v1/organizations/{org_id}/members)

**Request**:
```json
{
  "email": "newmember@acme.com",
  "role": "member"
}
```

**Response** (201 Created):
```json
{
  "user_id": "user_def789",
  "email": "newmember@acme.com",
  "full_name": "Carol Member",
  "role": "member",
  "joined_at": "2025-12-30T16:00:00Z",
  "invitation_sent": true
}
```

**Authorization**: Organization owner or admin

**Business Rules**:
- User must already exist in system (registered)
- Check max_members limit for plan tier
- Cannot add if already a member
- Email notification sent to invited user

**Errors**:
- 404 Not Found - User with email doesn't exist
- 409 Conflict - User already a member
- 403 Forbidden - Max members limit reached for plan tier

---

### 8. Remove Organization Member (DELETE /api/v1/organizations/{org_id}/members/{user_id})

**Response** (200 OK):
```json
{
  "message": "Member removed successfully",
  "user_id": "user_def789"
}
```

**Authorization**: Organization owner or admin

**Business Rules**:
- Cannot remove the owner
- Owner can remove anyone
- Admin can remove members (not owner, not other admins)
- Member cannot remove anyone
- User's access to org resources revoked immediately

**Errors**:
- 403 Forbidden - Cannot remove owner
- 403 Forbidden - Admin cannot remove other admins
- 404 Not Found - User not a member

---

### 9. Update Member Role (PATCH /api/v1/organizations/{org_id}/members/{user_id})

**Request**:
```json
{
  "role": "admin"
}
```

**Response** (200 OK):
```json
{
  "user_id": "user_abc456",
  "email": "member@acme.com",
  "full_name": "Dave Member",
  "role": "admin",
  "joined_at": "2025-12-30T13:00:00Z",
  "updated_at": "2025-12-30T17:00:00Z"
}
```

**Authorization**: Organization owner only

**Roles**:
- `owner` - Full control (cannot be assigned, only transferred)
- `admin` - Manage members, cases, settings (except billing)
- `member` - View and create cases, participate in investigations

**Business Rules**:
- Only owner can change roles
- Cannot change owner role via this endpoint (use transfer ownership)
- Revoke JWT tokens on role change (stale permissions)

---

### 10. Get Organization Settings (GET /api/v1/organizations/{org_id}/settings)

**Response** (200 OK):
```json
{
  "organization_id": "org_a1b2c3d4e5f6g7h8i",
  "plan_tier": "pro",
  "max_members": 50,
  "max_cases_per_month": 500,
  "max_storage_gb": 100,
  "features": {
    "knowledge_base": true,
    "ai_agents": true,
    "advanced_analytics": true,
    "priority_support": true,
    "sso": false
  },
  "settings": {
    "allow_public_cases": false,
    "require_2fa": true,
    "session_timeout_minutes": 60,
    "default_case_priority": "medium"
  }
}
```

**Authorization**: Organization member (any role)

---

### 11. Update Organization Settings (PATCH /api/v1/organizations/{org_id}/settings)

**Request**:
```json
{
  "allow_public_cases": true,
  "require_2fa": false,
  "session_timeout_minutes": 120
}
```

**Response** (200 OK):
```json
{
  "organization_id": "org_a1b2c3d4e5f6g7h8i",
  "settings": {
    "allow_public_cases": true,
    "require_2fa": false,
    "session_timeout_minutes": 120,
    "default_case_priority": "medium"
  },
  "updated_at": "2025-12-30T18:00:00Z"
}
```

**Authorization**: Organization owner only

**Updatable Settings**:
- `allow_public_cases` (bool)
- `require_2fa` (bool)
- `session_timeout_minutes` (int, 15-480)
- `default_case_priority` ("low", "medium", "high", "critical")

---

## Implementation Plan

### Step 1: API Service Layer (faultmaven/api/services/organization_api_service.py)

Create `APIOrganizationService` that wraps `OrganizationService` with API-specific logic:

```python
class APIOrganizationService:
    """API service layer for organization management."""

    def __init__(
        self,
        organization_service: OrganizationService,
        user_service: UserService,
    ):
        self.organization_service = organization_service
        self.user_service = user_service

    async def create_organization(
        self,
        name: str,
        slug: str,
        creator_user_id: str,
        description: Optional[str] = None,
        plan_tier: str = "free",
    ) -> Organization:
        """Create organization and add creator as owner."""
        # Validate plan tier
        # Create organization via domain service
        # Add creator as owner member
        # Return organization

    async def list_user_organizations(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[Organization], int]:
        """List organizations user is a member of."""

    async def get_organization(
        self,
        organization_id: str,
        user_id: str,
    ) -> Organization:
        """Get organization details (check member access)."""

    async def update_organization(
        self,
        organization_id: str,
        user_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Organization:
        """Update organization (owner only)."""

    async def delete_organization(
        self,
        organization_id: str,
        user_id: str,
    ):
        """Delete organization (owner only, soft delete)."""

    async def add_member(
        self,
        organization_id: str,
        requesting_user_id: str,
        email: str,
        role: str = "member",
    ) -> OrganizationMember:
        """Add member to organization (owner/admin only)."""

    async def remove_member(
        self,
        organization_id: str,
        requesting_user_id: str,
        user_id: str,
    ):
        """Remove member from organization (owner/admin only)."""

    async def update_member_role(
        self,
        organization_id: str,
        requesting_user_id: str,
        user_id: str,
        role: str,
    ) -> OrganizationMember:
        """Update member role (owner only)."""
```

### Step 2: API Routes (faultmaven/api/routes/organizations.py)

Create FastAPI router with all 11 endpoints:

```python
from fastapi import APIRouter, Depends, status
from faultmaven.api.middleware.auth import get_current_user
from faultmaven.api.services.organization_api_service import APIOrganizationService
from faultmaven.api.dependencies import get_api_organization_service

router = APIRouter(prefix="/api/v1/organizations", tags=["organizations"])

@router.post("", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED)
async def create_organization(
    request: OrganizationCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    org_service: APIOrganizationService = Depends(get_api_organization_service),
):
    """Create a new organization (user becomes owner)."""
    # Implementation

@router.get("", response_model=OrganizationListResponse)
async def list_user_organizations(
    limit: int = Query(20, le=100),
    offset: int = Query(0, ge=0),
    current_user: AuthenticatedUser = Depends(get_current_user),
    org_service: APIOrganizationService = Depends(get_api_organization_service),
):
    """List organizations user is a member of."""
    # Implementation

# ... 9 more endpoints
```

### Step 3: Request/Response Models (faultmaven/api/models/organization_models.py)

Pydantic models for API contracts:

```python
from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import datetime

class OrganizationCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=3, max_length=50, regex="^[a-z0-9-]+$")
    description: Optional[str] = Field(None, max_length=500)
    plan_tier: str = Field("free", regex="^(free|pro|enterprise)$")

class OrganizationResponse(BaseModel):
    organization_id: str
    name: str
    slug: str
    description: Optional[str]
    plan_tier: str
    max_members: int
    current_member_count: int
    owner_user_id: str
    created_at: datetime
    updated_at: datetime

class OrganizationMemberResponse(BaseModel):
    user_id: str
    email: str
    full_name: str
    role: str
    joined_at: datetime
```

### Step 4: Authorization Helpers

Add organization permission checks:

```python
# faultmaven/api/middleware/auth.py

async def require_org_owner(
    org_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    org_service: OrganizationService = Depends(get_organization_service),
) -> AuthenticatedUser:
    """Require user to be organization owner."""
    member = await org_service.get_member(org_id, current_user.user_id)
    if not member or member.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Organization owner access required"
        )
    return current_user

async def require_org_admin(
    org_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    org_service: OrganizationService = Depends(get_organization_service),
) -> AuthenticatedUser:
    """Require user to be organization owner or admin."""
    member = await org_service.get_member(org_id, current_user.user_id)
    if not member or member.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Organization admin access required"
        )
    return current_user

async def require_org_member(
    org_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    org_service: OrganizationService = Depends(get_organization_service),
) -> AuthenticatedUser:
    """Require user to be organization member."""
    member = await org_service.get_member(org_id, current_user.user_id)
    if not member:
        raise HTTPException(
            status_code=403,
            detail="Organization membership required"
        )
    return current_user
```

---

## Testing Requirements

### 1. API Service Layer Tests (tests/unit/api/services/test_organization_api_service.py)

**Test Coverage** (30-40 tests):

#### create_organization() Tests
- Creates organization successfully
- Adds creator as owner member
- Validates plan tier enum
- Validates slug format (lowercase, hyphens)
- Raises ValidationException on duplicate slug
- Raises ValidationException on invalid slug format

#### list_user_organizations() Tests
- Returns organizations user is a member of
- Pagination works (limit, offset)
- Returns empty list if no memberships
- Returns (organizations, total_count) tuple

#### get_organization() Tests
- Returns organization if user is member
- Raises AuthorizationError if not a member
- Raises NotFoundError if organization doesn't exist

#### update_organization() Tests
- Owner can update name and description
- Admin CANNOT update (raises AuthorizationError)
- Member CANNOT update (raises AuthorizationError)
- Raises NotFoundError if organization doesn't exist

#### delete_organization() Tests
- Owner can soft-delete organization
- Admin CANNOT delete (raises AuthorizationError)
- Raises ConflictError if active cases exist
- Removes all members on delete

#### add_member() Tests
- Owner can add members with any role
- Admin can add members (not admins)
- Member CANNOT add members (raises AuthorizationError)
- Checks max_members limit (raises ConflictError)
- User must exist (raises NotFoundError)
- User cannot already be member (raises ConflictError)

#### remove_member() Tests
- Owner can remove any member (except self)
- Admin can remove members (not owner, not other admins)
- Member CANNOT remove anyone
- Cannot remove owner (raises AuthorizationError)
- Raises NotFoundError if user not a member

#### update_member_role() Tests
- Owner can update any member's role
- Admin CANNOT update roles (raises AuthorizationError)
- Cannot set role to "owner" (use transfer ownership)
- Revokes JWT tokens on role change

**Expected Tests**: 30-40 tests

---

### 2. API Endpoint Tests (tests/integration/api/test_organizations_api.py)

**Test Coverage** (50-60 tests):

#### POST /api/v1/organizations
- 201 Created - creates organization successfully
- Creator becomes owner member
- Returns organization with all fields
- 422 Unprocessable Entity - invalid slug format
- 409 Conflict - duplicate slug
- 401 Unauthorized - no JWT token

#### GET /api/v1/organizations
- 200 OK - returns user's organizations
- Pagination works (limit, offset)
- Returns empty list if no memberships
- Shows user's role in each organization
- 401 Unauthorized - no JWT token

#### GET /api/v1/organizations/{org_id}
- 200 OK - member can view organization details
- Includes settings and member count
- 404 Not Found - organization doesn't exist
- 403 Forbidden - user not a member
- 401 Unauthorized - no JWT token

#### PATCH /api/v1/organizations/{org_id}
- 200 OK - owner can update name/description
- 403 Forbidden - admin cannot update
- 403 Forbidden - member cannot update
- 404 Not Found - organization doesn't exist
- 401 Unauthorized - no JWT token

#### DELETE /api/v1/organizations/{org_id}
- 200 OK - owner can delete organization
- 403 Forbidden - admin cannot delete
- 409 Conflict - active cases exist
- 404 Not Found - organization doesn't exist
- 401 Unauthorized - no JWT token

#### GET /api/v1/organizations/{org_id}/members
- 200 OK - member can list all members
- Pagination works
- Filter by role works
- 403 Forbidden - non-member cannot list
- 401 Unauthorized - no JWT token

#### POST /api/v1/organizations/{org_id}/members
- 201 Created - owner adds member
- 201 Created - admin adds member (not admin role)
- 403 Forbidden - admin cannot add admin
- 403 Forbidden - member cannot add
- 404 Not Found - user email doesn't exist
- 409 Conflict - user already a member
- 403 Forbidden - max members limit reached
- 401 Unauthorized - no JWT token

#### DELETE /api/v1/organizations/{org_id}/members/{user_id}
- 200 OK - owner removes member
- 200 OK - admin removes member (not admin/owner)
- 403 Forbidden - admin cannot remove admin
- 403 Forbidden - cannot remove owner
- 403 Forbidden - member cannot remove
- 404 Not Found - user not a member
- 401 Unauthorized - no JWT token

#### PATCH /api/v1/organizations/{org_id}/members/{user_id}
- 200 OK - owner updates member role
- Tokens revoked on role change
- 403 Forbidden - admin cannot update roles
- 403 Forbidden - member cannot update roles
- 422 Unprocessable Entity - invalid role
- 404 Not Found - user not a member
- 401 Unauthorized - no JWT token

#### GET /api/v1/organizations/{org_id}/settings
- 200 OK - member can view settings
- Includes plan limits and features
- 403 Forbidden - non-member cannot view
- 401 Unauthorized - no JWT token

#### PATCH /api/v1/organizations/{org_id}/settings
- 200 OK - owner updates settings
- 403 Forbidden - admin cannot update settings
- 422 Unprocessable Entity - invalid setting value
- 401 Unauthorized - no JWT token

**Expected Tests**: 50-60 tests

---

### 3. Authorization Tests (tests/integration/api/test_organization_authorization.py)

**Test Coverage** (20-25 tests):

#### Organization-Level Authorization
- Owner has full access to all endpoints
- Admin has limited access (no update/delete org, no update settings, no change roles)
- Member has read-only access (no write operations)
- Non-member has no access (403 on all org endpoints)

#### Multi-Tenant Isolation
- User A cannot access User B's organization
- list_organizations only shows user's memberships
- get_organization enforces membership check
- Member operations enforce organization membership

#### Plan Tier Limits
- Free plan: max 5 members
- Pro plan: max 50 members
- Enterprise plan: unlimited members
- Adding member beyond limit returns 403

**Expected Tests**: 20-25 tests

---

### 4. Multi-Tenant Isolation Tests (tests/integration/test_multi_tenant_isolation.py)

**Test Coverage** (15-20 tests):

#### Data Isolation
- Cases belong to organization
- Sessions belong to organization
- Evidence belongs to organization
- Knowledge items belong to organization
- User A cannot access User B's organization cases

#### Cross-Organization Leaks
- list_cases filters by current_user.organization_id
- get_case checks organization_id match
- update_case checks organization_id match
- delete_case checks organization_id match

**Expected Tests**: 15-20 tests

---

## Acceptance Criteria

### Functional Requirements

1. ✅ **11 organization API endpoints** implemented
2. ✅ **Organization CRUD** (create, read, update, delete)
3. ✅ **Member management** (add, remove, update role)
4. ✅ **Organization settings** (get, update)
5. ✅ **Multi-tenant isolation** (users only see their org data)
6. ✅ **Role-based permissions** (owner, admin, member)
7. ✅ **Plan tier limits** enforced (max_members)
8. ✅ **JWT token revocation** on role changes
9. ✅ **Soft delete** for organizations

### Testing Requirements

1. ✅ **API service tests**: 30-40 tests covering all operations
2. ✅ **API endpoint tests**: 50-60 tests covering all 11 endpoints
3. ✅ **Authorization tests**: 20-25 tests (owner/admin/member permissions)
4. ✅ **Multi-tenant isolation tests**: 15-20 tests (data isolation)
5. ✅ **Test coverage**: 90%+ for all new code
6. ✅ **No regression**: Existing tests still pass

### Code Quality

1. ✅ Clean separation of concerns (API service ← Domain service ← Repository)
2. ✅ Comprehensive docstrings for all public methods
3. ✅ Type hints on all function signatures
4. ✅ Proper error handling (ValidationException, AuthorizationError, NotFoundError, ConflictError)
5. ✅ Consistent naming conventions (snake_case for Python)
6. ✅ Request/response models follow existing patterns (TASK-017, TASK-019)

---

## Deliverables

1. **Code Files** (New):
   - `faultmaven/api/services/organization_api_service.py` - API service layer
   - `faultmaven/api/routes/organizations.py` - FastAPI routes (11 endpoints)
   - `faultmaven/api/models/organization_models.py` - Request/response models
   - `faultmaven/api/middleware/auth.py` - Organization permission helpers (require_org_owner, require_org_admin, require_org_member)
   - `faultmaven/api/dependencies.py` - Add get_api_organization_service dependency

2. **Test Files** (New):
   - `tests/unit/api/services/test_organization_api_service.py` - API service tests (30-40 tests)
   - `tests/integration/api/test_organizations_api.py` - API endpoint tests (50-60 tests)
   - `tests/integration/api/test_organization_authorization.py` - Authorization tests (20-25 tests)
   - `tests/integration/test_multi_tenant_isolation.py` - Multi-tenant tests (15-20 tests)

3. **Documentation**:
   - Update `faultmaven/api/routes/__init__.py` to export `organizations_router`
   - Update `faultmaven/main.py` to include organizations router
   - OpenAPI docs automatically updated by FastAPI

4. **Pull Request**:
   - Title: "feat: implement organization management API endpoints (TASK-021)"
   - Description: Full feature implementation with 11 endpoints
   - Link to TASK-021.md
   - Test coverage report showing 90%+

---

## Dependencies

### Required Services
- ✅ OrganizationService (domain service - already exists)
- ✅ UserService (TASK-018 - already exists)
- ✅ AuthService (TASK-017 - already exists)
- ✅ JWT authentication middleware (TASK-017 - already exists)

### External Dependencies
- FastAPI (existing)
- Pydantic (existing)
- SQLAlchemy (existing)

### New Dependencies
- None

---

## Security Considerations

### 1. Multi-Tenant Isolation
- All organization endpoints check membership
- Cases/sessions/evidence filtered by organization_id
- No cross-organization data leakage

### 2. Role-Based Permissions
- Owner: Full control
- Admin: Member management, case management (no org settings, no delete org)
- Member: Read-only organization access, can create/view cases

### 3. Input Validation
- Slug format validation (lowercase, hyphens only)
- Plan tier validation (free, pro, enterprise)
- Role validation (owner, admin, member)
- Email validation for member invitations

### 4. Token Revocation
- Role changes revoke all user JWT tokens
- Removal from organization revokes tokens
- Prevents stale permission access

### 5. Soft Delete
- Organizations soft-deleted (deleted_at timestamp)
- Cannot delete if active cases exist
- Prevents accidental data loss

---

## Non-Goals (Out of Scope)

1. ❌ **Team management** - Separate TASK-022 (teams are sub-groups within organizations)
2. ❌ **Billing integration** - Future task (Stripe integration)
3. ❌ **Organization transfer ownership** - Future enhancement
4. ❌ **Organization merging** - Future enhancement
5. ❌ **SSO/SAML** - Enterprise feature (future)
6. ❌ **API keys for organizations** - Future task
7. ❌ **Audit logs** - Future enhancement
8. ❌ **Email notifications** - Future task (member invitation emails)

---

## Success Criteria

**APPROVED if**:
- ✅ All 11 organization API endpoints implemented
- ✅ API service layer properly separates API logic from domain logic
- ✅ 115-145 tests total (API service: 30-40, endpoints: 50-60, authorization: 20-25, isolation: 15-20)
- ✅ Test coverage 90%+ for all new code
- ✅ Multi-tenant isolation verified (no cross-org data leakage)
- ✅ Role-based permissions enforced (owner/admin/member)
- ✅ Plan tier limits enforced (max_members)
- ✅ JWT token revocation on role changes verified
- ✅ No regression in existing tests

**REQUEST CHANGES if**:
- ❌ Missing endpoints (less than 11)
- ❌ Authorization tests incomplete
- ❌ Multi-tenant isolation not verified
- ❌ Test coverage below 90%
- ❌ Cross-organization data leaks found
- ❌ Existing tests broken

---

## Design References

- **TASK-017**: JWT Authentication & Authorization Middleware (permission patterns)
- **TASK-018**: User Management Service (service layer patterns)
- **TASK-019**: Admin User Management (admin endpoint patterns, testing approach)
- **OrganizationService**: Domain service implementation (existing)
- **Principle**: Clean separation - API service wraps domain service with API-specific logic

---

## Notes

- OrganizationService already exists with core business logic
- This task focuses on API layer (routes, API service, models)
- Multi-tenant isolation is critical for production readiness
- Plan tier limits ensure monetization strategy
- Role-based permissions follow principle of least privilege

---

**Estimated Effort**: 6-8 hours (3-4 hours implementation + 3-4 hours testing)
**Test-Engineer Review**: Required (verify authorization, multi-tenant isolation, 90%+ coverage)
