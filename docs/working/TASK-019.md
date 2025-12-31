# TASK-019: Admin User Management Endpoints

## Task Metadata
- **Phase**: Week 6, Day 7-8 (User Management)
- **Priority**: P0 (Complete authentication foundation)
- **Estimated Time**: 4-6 hours
- **Dependencies**: TASK-018 (User Management Service - PR #19)
- **Assignee**: Developer
- **Reports To**: Solutions Architect

## Objective

**Implement admin-only user management endpoints** to complete the authentication foundation:

1. **User listing** with pagination and filtering (admin only)
2. **User detail retrieval** by ID (admin only)
3. **User activation/deactivation** (admin only)
4. **User role management** (assign/remove roles - admin only)
5. **Organization user management** (list users in organization)
6. **Comprehensive testing** (90%+ coverage)

---

## Context

TASK-018 implemented core user management (registration, authentication, password management, profile updates). TASK-019 completes the authentication foundation by adding **administrative user management capabilities**.

**Authentication Architecture**:
- TASK-017: JWT authentication with RBAC (3 roles: admin, member, viewer)
- TASK-018: User registration, authentication, password management
- TASK-019: Admin user management (this task)

**Admin Capabilities**:
- List all users in organization (paginated, filtered)
- View detailed user information
- Activate/deactivate user accounts
- Assign/remove roles (admin, member, viewer)
- Prevent organization-level privilege escalation

---

## Requirements

### 1. User List Endpoint

**Endpoint**: `GET /api/v1/admin/users`

**Authentication**: JWT Bearer token (admin role required)

**Query Parameters**:
```python
is_active: Optional[bool] = None  # Filter by active/inactive
role: Optional[str] = None        # Filter by role (admin, member, viewer)
search: Optional[str] = None      # Search email or full_name (case-insensitive)
limit: int = 50                   # Max results per page (default 50, max 100)
offset: int = 0                   # Pagination offset
```

**Response** (200 OK):
```json
{
  "users": [
    {
      "user_id": "uuid",
      "organization_id": "uuid",
      "email": "admin@example.com",
      "full_name": "Admin User",
      "roles": ["admin"],
      "is_active": true,
      "is_verified": true,
      "last_login_at": "2025-12-30T10:00:00Z",
      "created_at": "2025-12-01T10:00:00Z",
      "updated_at": "2025-12-30T10:00:00Z"
    }
  ],
  "total": 42,
  "limit": 50,
  "offset": 0
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token
- `403 Forbidden`: User lacks admin role
- `422 Unprocessable Entity`: Invalid query parameters

**Business Rules**:
- Admin can only see users in their own organization
- Pagination limit capped at 100 to prevent performance issues
- Search matches email OR full_name (case-insensitive, partial match)
- Results sorted by created_at DESC (newest first)

---

### 2. User Detail Endpoint

**Endpoint**: `GET /api/v1/admin/users/{user_id}`

**Authentication**: JWT Bearer token (admin role required)

**Response** (200 OK):
```json
{
  "user_id": "uuid",
  "organization_id": "uuid",
  "email": "user@example.com",
  "full_name": "Test User",
  "roles": ["member"],
  "permissions": ["cases:read", "cases:write", "sessions:create", ...],
  "is_active": true,
  "is_verified": true,
  "last_login_at": "2025-12-30T10:00:00Z",
  "created_at": "2025-12-01T10:00:00Z",
  "updated_at": "2025-12-30T10:00:00Z",
  "metadata": {
    "login_count": 42,
    "failed_login_attempts": 0
  }
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token
- `403 Forbidden`: User lacks admin role OR user belongs to different organization
- `404 Not Found`: User does not exist

**Business Rules**:
- Admin can only view users in their own organization
- Response includes derived permissions from roles
- NEVER return hashed_password field
- Include metadata for audit purposes

---

### 3. User Activation/Deactivation Endpoints

#### 3a. Deactivate User

**Endpoint**: `POST /api/v1/admin/users/{user_id}/deactivate`

**Authentication**: JWT Bearer token (admin role required)

**Response** (200 OK):
```json
{
  "user_id": "uuid",
  "is_active": false,
  "updated_at": "2025-12-30T10:30:00Z",
  "message": "User deactivated successfully. All JWT tokens revoked."
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token
- `403 Forbidden`: User lacks admin role OR trying to deactivate self
- `404 Not Found`: User does not exist
- `409 Conflict`: User already deactivated

**Business Rules**:
- Admin cannot deactivate themselves (prevent lockout)
- Deactivation sets `is_active=False`
- All user's JWT tokens revoked (access + refresh)
- User cannot log in after deactivation
- Deactivated users still visible in user list (soft delete)

#### 3b. Activate User

**Endpoint**: `POST /api/v1/admin/users/{user_id}/activate`

**Authentication**: JWT Bearer token (admin role required)

**Response** (200 OK):
```json
{
  "user_id": "uuid",
  "is_active": true,
  "updated_at": "2025-12-30T10:30:00Z",
  "message": "User activated successfully."
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token
- `403 Forbidden`: User lacks admin role
- `404 Not Found`: User does not exist
- `409 Conflict`: User already active

**Business Rules**:
- Only admins can activate users
- Activation sets `is_active=True`
- User can log in after activation
- Activation does NOT generate new JWT tokens (user must log in)

---

### 4. Role Management Endpoints

#### 4a. Assign Role

**Endpoint**: `POST /api/v1/admin/users/{user_id}/roles`

**Authentication**: JWT Bearer token (admin role required)

**Request Body**:
```json
{
  "role": "member"  // "admin", "member", or "viewer"
}
```

**Response** (200 OK):
```json
{
  "user_id": "uuid",
  "roles": ["member"],
  "updated_at": "2025-12-30T10:30:00Z",
  "message": "Role 'member' assigned successfully. All JWT tokens revoked."
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token
- `403 Forbidden`: User lacks admin role OR trying to modify own roles
- `404 Not Found`: User does not exist
- `422 Unprocessable Entity`: Invalid role
- `409 Conflict`: User already has this role

**Business Rules**:
- Admin cannot modify their own roles (prevent privilege escalation/lockout)
- Role assignment replaces ALL existing roles (single role per user)
- All user's JWT tokens revoked (roles changed, tokens stale)
- User must log in again to get new tokens with updated roles
- Valid roles: `admin`, `member`, `viewer` (from TASK-017 Role enum)

#### 4b. Remove Role (Downgrade to Viewer)

**Endpoint**: `DELETE /api/v1/admin/users/{user_id}/roles/{role}`

**Authentication**: JWT Bearer token (admin role required)

**Response** (200 OK):
```json
{
  "user_id": "uuid",
  "roles": ["viewer"],
  "updated_at": "2025-12-30T10:30:00Z",
  "message": "Role 'admin' removed. User downgraded to 'viewer'. All JWT tokens revoked."
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token
- `403 Forbidden`: User lacks admin role OR trying to remove own admin role
- `404 Not Found`: User does not exist OR user doesn't have this role
- `422 Unprocessable Entity`: Invalid role

**Business Rules**:
- Admin cannot remove their own admin role (prevent lockout)
- Removing a role downgrades user to `viewer` (default minimum privilege)
- All user's JWT tokens revoked
- Cannot remove `viewer` role (minimum privilege level)

---

### 5. Organization User Management

**Endpoint**: `GET /api/v1/users`

**Authentication**: JWT Bearer token (any authenticated user)

**Query Parameters**:
```python
limit: int = 50   # Max results per page (default 50, max 100)
offset: int = 0   # Pagination offset
```

**Response** (200 OK):
```json
{
  "users": [
    {
      "user_id": "uuid",
      "email": "user@example.com",
      "full_name": "User Name",
      "roles": ["member"],
      "is_active": true
    }
  ],
  "total": 10,
  "limit": 50,
  "offset": 0
}
```

**Error Responses**:
- `401 Unauthorized`: No valid JWT token

**Business Rules**:
- ANY authenticated user can list users in their organization
- Returns limited user info (no metadata, permissions, timestamps)
- Automatically filtered by authenticated user's organization_id
- Only shows active users (is_active=True)
- Useful for @mentions, assignment, collaboration features

---

## Implementation Details

### API Route Structure

```python
# faultmaven/api/routes/admin.py (NEW FILE)

from fastapi import APIRouter, Depends, Query, Path
from faultmaven.api.middleware.auth import require_admin, get_current_user
from faultmaven.api.models import (
    UserListResponse,
    UserDetailResponse,
    UserStatusResponse,
    RoleAssignmentRequest,
    RoleAssignmentResponse,
)
from faultmaven.services.user_service import UserService

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin - User Management"],
)

@router.get("/users", response_model=UserListResponse)
async def list_users(
    current_user: AuthenticatedUser = Depends(require_admin),
    is_active: Optional[bool] = Query(None),
    role: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    user_service: UserService = Depends(get_user_service),
) -> UserListResponse:
    """List all users in organization (admin only)."""
    ...

@router.get("/users/{user_id}", response_model=UserDetailResponse)
async def get_user_details(
    user_id: str = Path(...),
    current_user: AuthenticatedUser = Depends(require_admin),
    user_service: UserService = Depends(get_user_service),
) -> UserDetailResponse:
    """Get detailed user information (admin only)."""
    ...

@router.post("/users/{user_id}/deactivate", response_model=UserStatusResponse)
async def deactivate_user(
    user_id: str = Path(...),
    current_user: AuthenticatedUser = Depends(require_admin),
    user_service: UserService = Depends(get_user_service),
) -> UserStatusResponse:
    """Deactivate user account (admin only)."""
    ...

@router.post("/users/{user_id}/activate", response_model=UserStatusResponse)
async def activate_user(
    user_id: str = Path(...),
    current_user: AuthenticatedUser = Depends(require_admin),
    user_service: UserService = Depends(get_user_service),
) -> UserStatusResponse:
    """Activate user account (admin only)."""
    ...

@router.post("/users/{user_id}/roles", response_model=RoleAssignmentResponse)
async def assign_role(
    user_id: str = Path(...),
    request: RoleAssignmentRequest = ...,
    current_user: AuthenticatedUser = Depends(require_admin),
    user_service: UserService = Depends(get_user_service),
) -> RoleAssignmentResponse:
    """Assign role to user (admin only)."""
    ...

@router.delete("/users/{user_id}/roles/{role}", response_model=RoleAssignmentResponse)
async def remove_role(
    user_id: str = Path(...),
    role: str = Path(...),
    current_user: AuthenticatedUser = Depends(require_admin),
    user_service: UserService = Depends(get_user_service),
) -> RoleAssignmentResponse:
    """Remove role from user (admin only)."""
    ...
```

```python
# faultmaven/api/routes/users.py (UPDATE EXISTING)

@router.get("/users", response_model=UserListResponse)
async def list_organization_users(
    current_user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    user_service: UserService = Depends(get_user_service),
) -> UserListResponse:
    """List users in current organization (any authenticated user)."""
    ...
```

---

### UserService Extensions

```python
# faultmaven/services/user_service.py (UPDATE EXISTING)

class UserService:
    """User management business logic (TASK-018, TASK-019)."""

    # TASK-019: Admin user management methods

    async def list_users(
        self,
        organization_id: str,
        is_active: Optional[bool] = None,
        role: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        """List users with filtering and pagination.

        Args:
            organization_id: Organization to filter by
            is_active: Filter by active status (None = all)
            role: Filter by role (admin, member, viewer)
            search: Search email or full_name (case-insensitive)
            limit: Max results (capped at 100)
            offset: Pagination offset

        Returns:
            Tuple of (users, total_count)
        """
        ...

    async def get_user_with_metadata(
        self,
        user_id: str,
        organization_id: str,
    ) -> Optional[dict]:
        """Get user with additional metadata.

        Returns user dict with:
        - All User fields
        - permissions (derived from roles)
        - metadata.login_count (if tracked)
        - metadata.failed_login_attempts (if tracked)
        """
        ...

    async def activate_user(
        self,
        user_id: str,
        organization_id: str,
    ) -> User:
        """Activate deactivated user.

        Raises:
            NotFoundError: User not found
            AuthorizationError: User belongs to different organization
            ConflictError: User already active
        """
        ...

    async def assign_role(
        self,
        user_id: str,
        role: str,
        organization_id: str,
        admin_user_id: str,
    ) -> User:
        """Assign role to user (replaces existing roles).

        Args:
            user_id: Target user ID
            role: Role to assign (admin, member, viewer)
            organization_id: Organization for authorization
            admin_user_id: Admin performing the action (cannot be same as user_id)

        Raises:
            NotFoundError: User not found
            AuthorizationError: User belongs to different organization OR admin_user_id == user_id
            ValidationException: Invalid role
            ConflictError: User already has this role
        """
        ...

    async def remove_role(
        self,
        user_id: str,
        role: str,
        organization_id: str,
        admin_user_id: str,
    ) -> User:
        """Remove role from user (downgrades to viewer).

        Args:
            user_id: Target user ID
            role: Role to remove (admin, member, viewer)
            organization_id: Organization for authorization
            admin_user_id: Admin performing the action (cannot be same as user_id)

        Raises:
            NotFoundError: User not found OR user doesn't have this role
            AuthorizationError: User belongs to different organization OR admin_user_id == user_id
            ValidationException: Attempting to remove viewer role (minimum privilege)
        """
        ...
```

---

### User Model Extensions

```python
# faultmaven/models/user.py (UPDATE EXISTING)

from faultmaven.models.auth import Role, ROLE_PERMISSIONS

class User:
    """User domain model (TASK-018, TASK-019)."""

    user_id: str
    organization_id: str
    email: str
    full_name: str
    hashed_password: str
    roles: list[str]  # NEW: List of role names ["admin"], ["member"], etc.
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    # TASK-019: Derive permissions from roles
    @property
    def permissions(self) -> list[str]:
        """Get all permissions from user's roles."""
        perms = set()
        for role in self.roles:
            if role in ROLE_PERMISSIONS:
                perms.update(ROLE_PERMISSIONS[role])
        return sorted(perms)

    def has_role(self, role: str) -> bool:
        """Check if user has specific role."""
        return role in self.roles

    def has_permission(self, permission: str) -> bool:
        """Check if user has specific permission."""
        return permission in self.permissions
```

---

### API Models

```python
# faultmaven/api/models.py (ADD NEW MODELS)

from pydantic import BaseModel, Field, EmailStr

class UserListItem(BaseModel):
    """User list item (limited info)."""
    user_id: str
    email: EmailStr
    full_name: str
    roles: list[str]
    is_active: bool

class UserListResponse(BaseModel):
    """User list response with pagination."""
    users: list[UserListItem]
    total: int
    limit: int
    offset: int

class UserDetailResponse(BaseModel):
    """Detailed user information (admin only)."""
    user_id: str
    organization_id: str
    email: EmailStr
    full_name: str
    roles: list[str]
    permissions: list[str]  # Derived from roles
    is_active: bool
    is_verified: bool
    last_login_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    metadata: dict = Field(default_factory=dict)

class UserStatusResponse(BaseModel):
    """User activation/deactivation response."""
    user_id: str
    is_active: bool
    updated_at: datetime
    message: str

class RoleAssignmentRequest(BaseModel):
    """Role assignment request."""
    role: str = Field(..., pattern="^(admin|member|viewer)$")

class RoleAssignmentResponse(BaseModel):
    """Role assignment response."""
    user_id: str
    roles: list[str]
    updated_at: datetime
    message: str
```

---

## Testing Requirements

### Test Coverage Target: 90%+

**Estimated Test Count**: 95-115 tests

### 1. UserService Admin Tests (30-40 tests)

**File**: `tests/unit/services/test_user_service_admin.py`

**Test Categories**:

#### list_users() Tests
- ✅ Returns all users in organization
- ✅ Pagination works (limit, offset)
- ✅ Filter by is_active=True
- ✅ Filter by is_active=False
- ✅ Filter by role (admin, member, viewer)
- ✅ Search by email (case-insensitive, partial match)
- ✅ Search by full_name (case-insensitive, partial match)
- ✅ Combined filters (active + role + search)
- ✅ Results sorted by created_at DESC
- ✅ Returns (users, total_count) tuple
- ✅ Respects organization boundary (no cross-org leaks)
- ✅ Empty list when no matches

#### get_user_with_metadata() Tests
- ✅ Returns user with all fields
- ✅ Includes derived permissions from roles
- ✅ Includes metadata (login_count, failed_attempts)
- ✅ Returns None if user not found
- ✅ Returns None if different organization

#### activate_user() Tests
- ✅ Activates deactivated user (is_active=False → True)
- ✅ Updates updated_at timestamp
- ✅ NotFoundError if user doesn't exist
- ✅ AuthorizationError if different organization
- ✅ ConflictError if already active

#### assign_role() Tests
- ✅ Assigns admin role
- ✅ Assigns member role
- ✅ Assigns viewer role
- ✅ Replaces existing roles (single role per user)
- ✅ Revokes all JWT tokens (calls AuthService.revoke_user_tokens)
- ✅ Updates updated_at timestamp
- ✅ NotFoundError if user doesn't exist
- ✅ AuthorizationError if different organization
- ✅ AuthorizationError if admin_user_id == user_id (self-modification)
- ✅ ValidationException on invalid role
- ✅ ConflictError if already has this role

#### remove_role() Tests
- ✅ Removes admin role, downgrades to viewer
- ✅ Removes member role, downgrades to viewer
- ✅ Revokes all JWT tokens
- ✅ Updates updated_at timestamp
- ✅ NotFoundError if user doesn't exist
- ✅ NotFoundError if user doesn't have this role
- ✅ AuthorizationError if different organization
- ✅ AuthorizationError if admin_user_id == user_id (self-modification)
- ✅ ValidationException when removing viewer role (minimum privilege)

---

### 2. Admin API Endpoint Tests (35-45 tests)

**File**: `tests/integration/api/test_admin_api.py`

**Test Categories**:

#### GET /api/v1/admin/users
- ✅ 200 OK returns user list (admin)
- ✅ Returns pagination info (total, limit, offset)
- ✅ Filter by is_active works
- ✅ Filter by role works
- ✅ Search by email works
- ✅ Search by full_name works
- ✅ Combined filters work
- ✅ Pagination (limit/offset) works
- ✅ Results sorted by created_at DESC
- ✅ 401 Unauthorized if no JWT token
- ✅ 403 Forbidden if not admin (member, viewer)
- ✅ 422 Unprocessable Entity on invalid query params

#### GET /api/v1/admin/users/{user_id}
- ✅ 200 OK returns user details (admin)
- ✅ Includes derived permissions
- ✅ Includes metadata
- ✅ Does NOT return hashed_password
- ✅ 404 Not Found if user doesn't exist
- ✅ 403 Forbidden if user in different organization
- ✅ 401 Unauthorized if no JWT token
- ✅ 403 Forbidden if not admin

#### POST /api/v1/admin/users/{user_id}/deactivate
- ✅ 200 OK deactivates user (admin)
- ✅ User is_active=False in database
- ✅ All JWT tokens revoked
- ✅ User cannot log in after deactivation
- ✅ Message confirms revocation
- ✅ 403 Forbidden when admin tries to deactivate self
- ✅ 404 Not Found if user doesn't exist
- ✅ 409 Conflict if already deactivated
- ✅ 401 Unauthorized if no JWT token
- ✅ 403 Forbidden if not admin

#### POST /api/v1/admin/users/{user_id}/activate
- ✅ 200 OK activates user (admin)
- ✅ User is_active=True in database
- ✅ User can log in after activation
- ✅ 404 Not Found if user doesn't exist
- ✅ 409 Conflict if already active
- ✅ 401 Unauthorized if no JWT token
- ✅ 403 Forbidden if not admin

#### POST /api/v1/admin/users/{user_id}/roles
- ✅ 200 OK assigns admin role
- ✅ 200 OK assigns member role
- ✅ 200 OK assigns viewer role
- ✅ Replaces existing roles
- ✅ All JWT tokens revoked
- ✅ User must re-login to get new tokens with updated roles
- ✅ Message confirms token revocation
- ✅ 403 Forbidden when admin tries to modify own roles
- ✅ 404 Not Found if user doesn't exist
- ✅ 422 Unprocessable Entity on invalid role
- ✅ 409 Conflict if already has this role
- ✅ 401 Unauthorized if no JWT token
- ✅ 403 Forbidden if not admin

#### DELETE /api/v1/admin/users/{user_id}/roles/{role}
- ✅ 200 OK removes admin role, downgrades to viewer
- ✅ 200 OK removes member role, downgrades to viewer
- ✅ All JWT tokens revoked
- ✅ User must re-login
- ✅ 403 Forbidden when admin tries to remove own admin role
- ✅ 404 Not Found if user doesn't exist
- ✅ 404 Not Found if user doesn't have this role
- ✅ 422 Unprocessable Entity when removing viewer role
- ✅ 401 Unauthorized if no JWT token
- ✅ 403 Forbidden if not admin

---

### 3. Organization User List Tests (10-15 tests)

**File**: `tests/integration/api/test_users_api.py` (update existing)

#### GET /api/v1/users
- ✅ 200 OK returns user list (any authenticated user)
- ✅ Returns limited info (no metadata, permissions)
- ✅ Filtered by authenticated user's organization
- ✅ Only shows active users (is_active=True)
- ✅ Pagination works (limit, offset)
- ✅ 401 Unauthorized if no JWT token
- ✅ Different users see different organization users

---

### 4. Authorization Tests (15-20 tests)

**File**: `tests/integration/api/test_admin_authorization.py`

**Test Categories**:

#### Admin-Only Enforcement
- ✅ Admin can list users
- ✅ Member CANNOT list users (403)
- ✅ Viewer CANNOT list users (403)
- ✅ Admin can view user details
- ✅ Member CANNOT view user details (403)
- ✅ Admin can deactivate users
- ✅ Member CANNOT deactivate users (403)
- ✅ Admin can activate users
- ✅ Member CANNOT activate users (403)
- ✅ Admin can assign roles
- ✅ Member CANNOT assign roles (403)
- ✅ Admin can remove roles
- ✅ Member CANNOT remove roles (403)

#### Self-Modification Prevention
- ✅ Admin CANNOT deactivate self (403)
- ✅ Admin CANNOT modify own roles (403)
- ✅ Admin CANNOT remove own admin role (403)
- ✅ Admin CAN deactivate other admins
- ✅ Admin CAN modify other admins' roles

#### Organization Boundary Enforcement
- ✅ Admin in Org A cannot see users in Org B
- ✅ Admin in Org A cannot deactivate users in Org B
- ✅ Admin in Org A cannot modify roles for users in Org B

---

## Acceptance Criteria

### Functional Requirements

1. ✅ **Admin user listing** works with pagination, filtering, search
2. ✅ **User detail retrieval** returns complete user info with permissions
3. ✅ **User activation/deactivation** works with token revocation
4. ✅ **Role assignment** replaces roles and revokes tokens
5. ✅ **Role removal** downgrades to viewer and revokes tokens
6. ✅ **Organization user list** available to all authenticated users
7. ✅ **Admin-only enforcement** prevents non-admins from admin endpoints
8. ✅ **Self-modification prevention** blocks admins from modifying themselves
9. ✅ **Organization boundaries** enforced (no cross-org access)

### Testing Requirements

1. ✅ **95+ tests** covering service layer, API endpoints, authorization
2. ✅ **90%+ test coverage** (pytest-cov)
3. ✅ All admin endpoints tested (list, get, activate, deactivate, assign, remove)
4. ✅ Authorization tested (admin-only, self-modification prevention, org boundaries)
5. ✅ Error scenarios tested (404, 403, 409, 422)
6. ✅ Token revocation verified (deactivation, role changes)

### Code Quality

1. ✅ Follow patterns from TASK-017/TASK-018
2. ✅ Clear error messages for authorization failures
3. ✅ Comprehensive docstrings
4. ✅ Type hints on all public methods
5. ✅ No duplicate code (DRY principle)

---

## Deliverables

1. **Code Files**:
   - `faultmaven/api/routes/admin.py` (new)
   - `faultmaven/api/routes/users.py` (update)
   - `faultmaven/services/user_service.py` (update)
   - `faultmaven/models/user.py` (update)
   - `faultmaven/api/models.py` (update)

2. **Test Files**:
   - `tests/unit/services/test_user_service_admin.py` (new)
   - `tests/integration/api/test_admin_api.py` (new)
   - `tests/integration/api/test_admin_authorization.py` (new)
   - `tests/integration/api/test_users_api.py` (update)

3. **Documentation**:
   - Update OpenAPI docs (automatic via FastAPI)
   - Add inline code comments for complex authorization logic

4. **Pull Request**:
   - Title: "TASK-019: Admin User Management Endpoints"
   - Description with testing summary
   - Link to TASK-019.md

---

## Dependencies

### Required Services (from previous tasks)
- ✅ TASK-017: JWT authentication with RBAC (Role enum, require_admin middleware)
- ✅ TASK-018: User model, UserRepository, UserService (register, authenticate, deactivate)

### External Dependencies
- FastAPI (existing)
- SQLAlchemy (existing)
- Pydantic (existing)

### New Dependencies
- None

---

## Security Considerations

### Critical Security Controls

1. **Admin-Only Enforcement**:
   - All `/api/v1/admin/*` endpoints require admin role
   - Use `Depends(require_admin)` from TASK-017

2. **Self-Modification Prevention**:
   - Admin cannot deactivate themselves (prevent lockout)
   - Admin cannot modify their own roles (prevent privilege loss)
   - Check `admin_user_id != user_id` in service layer

3. **Organization Boundaries**:
   - All operations filtered by `organization_id`
   - Prevent cross-organization user access
   - Admin in Org A cannot see/modify users in Org B

4. **Token Revocation**:
   - Deactivation revokes all JWT tokens (access + refresh)
   - Role changes revoke all JWT tokens (stale permissions)
   - Force re-login after privilege changes

5. **Audit Trail**:
   - Log all admin actions (user activation, role changes)
   - Include admin_user_id in logs for accountability

6. **Input Validation**:
   - Role must be valid enum value (admin, member, viewer)
   - Pagination limits capped at 100
   - Search queries sanitized (no SQL injection)

---

## Non-Goals (Out of Scope)

1. ❌ **Multi-role support** - Users have single role (TASK-017 design)
2. ❌ **Custom permissions** - Use predefined role permissions only
3. ❌ **User deletion** - Only deactivation (soft delete), no hard delete
4. ❌ **Bulk operations** - No bulk activate/deactivate/role assignment
5. ❌ **User import/export** - No CSV import/export functionality
6. ❌ **Password reset by admin** - Admins cannot reset user passwords (security)
7. ❌ **Email verification by admin** - No admin override for email verification
8. ❌ **Login as user** - No impersonation/sudo functionality

---

## Success Criteria

**APPROVED if**:
- ✅ All 6 admin endpoints implemented and tested
- ✅ Organization user list endpoint updated
- ✅ 95+ tests with 90%+ coverage
- ✅ Admin-only enforcement works (403 for non-admins)
- ✅ Self-modification prevention works (403 for self-deactivate/role-change)
- ✅ Organization boundaries enforced (no cross-org access)
- ✅ Token revocation works (deactivation, role changes)
- ✅ All error scenarios return correct HTTP status codes
- ✅ Code quality matches TASK-017/TASK-018 patterns
- ✅ Security review passes (no privilege escalation, lockout prevention)

**REQUEST CHANGES if**:
- ❌ Admin endpoints allow non-admin access
- ❌ Self-modification prevention missing (admin can deactivate self)
- ❌ Cross-organization access possible
- ❌ Token revocation not working (stale tokens accepted)
- ❌ Test coverage below 90%
- ❌ Missing critical authorization tests

---

## Design References

- **TASK-017**: JWT Authentication & Authorization Middleware (Role enum, require_admin)
- **TASK-018**: User Management Service (User model, UserService, UserRepository)
- **Architecture**: docs/architecture/AUTH_ARCHITECTURE.md (RBAC design)

---

## Notes

- This task completes the authentication foundation for FaultMaven
- After TASK-019, we have complete user management: registration → authentication → password management → admin controls
- Future tasks will integrate this auth system into other modules (cases, sessions, evidence)
- Admin user management is P0 for multi-tenant SaaS deployment

---

**Estimated Effort**: 4-6 hours (2-3 hours implementation + 2-3 hours testing)
**Test-Engineer Review**: Required (90%+ coverage, authorization testing critical)
