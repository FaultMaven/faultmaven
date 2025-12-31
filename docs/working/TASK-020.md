# TASK-020: Remove Legacy Header Authentication (Technical Debt Cleanup)

## Task Metadata
- **Phase**: Week 7, Day 1 (Technical Debt Cleanup)
- **Priority**: P1 (Production readiness - remove unnecessary complexity)
- **Estimated Time**: 2-3 hours
- **Dependencies**: TASK-019 (Admin User Management - PR #20)
- **Assignee**: Developer
- **Reports To**: Solutions Architect

## Objective

**Remove unnecessary "legacy header" authentication support** from the codebase to eliminate technical debt created during initial development:

1. **Remove `get_auth_context()` dual-mode helper** from all API route files
2. **Replace with direct JWT authentication** using `get_current_user()` dependency
3. **Remove X-Organization-ID and X-User-ID header support** entirely
4. **Update all tests** to use JWT tokens only
5. **Simplify authentication** to single clean pattern: JWT Bearer tokens

---

## Context

### Problem: Unnecessary Backwards Compatibility

During TASK-016/TASK-017, a "backwards-compatible" authentication pattern was implemented:

```python
# Current implementation (UNNECESSARY COMPLEXITY)
async def get_auth_context(
    current_user: Optional[AuthenticatedUser] = Depends(get_current_user_optional),
    legacy_org_id: Optional[str] = Header(None, alias="X-Organization-ID"),
    legacy_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> tuple[str, str]:
    """Support both JWT and legacy headers."""
    if current_user:
        return current_user.organization_id, current_user.user_id
    if legacy_org_id and legacy_user_id:
        return legacy_org_id, legacy_user_id
    # ...
```

**Why This Is Wrong**:
- ❌ No legacy clients exist (greenfield system)
- ❌ Creates unnecessary code complexity
- ❌ Provides two authentication paths (increases attack surface)
- ❌ Confuses API documentation
- ❌ Technical debt from day one

### Solution: JWT-Only Authentication

**Clean Implementation**:
```python
# Simplified JWT-only approach
from faultmaven.api.middleware.auth import get_current_user

@router.post("/cases")
async def create_case(
    request: CaseCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    case_service: APICaseService = Depends(get_api_case_service),
):
    """Create case (requires authentication)."""
    # No tuple unpacking - direct access to authenticated user
    case = await case_service.create_case(
        organization_id=current_user.organization_id,
        created_by=current_user.user_id,
        title=request.title,
        description=request.description,
    )
    return CaseResponse.from_domain(case)
```

**Benefits**:
- ✅ Single authentication path (JWT only)
- ✅ Cleaner code (no tuple unpacking)
- ✅ Smaller attack surface
- ✅ Clearer API documentation
- ✅ No technical debt

---

## Files To Update

### 1. API Route Files (Remove `get_auth_context()`)

**Files**:
- `faultmaven/api/routes/cases.py`
- `faultmaven/api/routes/sessions.py`
- `faultmaven/api/routes/evidence.py`
- `faultmaven/api/routes/agent.py`

**Changes**:

#### Before (Dual-Mode):
```python
async def get_auth_context(...) -> tuple[str, str]:
    """Support both JWT and legacy headers."""
    ...

@router.post("/cases")
async def create_case(
    request: CaseCreateRequest,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    ...
):
    organization_id, user_id = auth_context
    ...
```

#### After (JWT-Only):
```python
# Remove get_auth_context() entirely

from faultmaven.api.middleware.auth import get_current_user

@router.post("/cases")
async def create_case(
    request: CaseCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    ...
):
    # Direct access to current_user attributes
    organization_id = current_user.organization_id
    user_id = current_user.user_id
    ...
```

---

### 2. Admin Routes (Already JWT-Only)

**File**: `faultmaven/api/routes/admin.py`

**Status**: ✅ Already uses `require_admin()` - no changes needed

```python
@router.get("/users")
async def list_users(
    current_user: AuthenticatedUser = Depends(require_admin),
    ...
):
    # Already correct - JWT-only
    ...
```

---

### 3. Auth Routes (Already JWT-Only)

**File**: `faultmaven/api/routes/auth.py`

**Status**: ✅ Public auth endpoints + JWT-protected endpoints - no changes needed

```python
@router.post("/login")
async def login(...):
    # Public endpoint - no auth required
    ...

@router.post("/logout")
async def logout(
    current_user: AuthenticatedUser = Depends(get_current_user),
    ...
):
    # Already correct - JWT-only
    ...
```

---

### 4. User Routes (Already JWT-Only)

**File**: `faultmaven/api/routes/users.py`

**Status**: ✅ Already uses `get_current_user()` - no changes needed

```python
@router.get("/me")
async def get_current_user_profile(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    # Already correct - JWT-only
    ...
```

---

## Detailed Implementation

### Pattern 1: Read Operations (cases:read, sessions:read, evidence:read)

**Current**:
```python
@router.get("/cases/{case_id}")
async def get_case(
    case_id: str,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
):
    organization_id, _ = auth_context
    case = await case_service.get_case(case_id, organization_id)
    return CaseResponse.from_domain(case)
```

**Corrected**:
```python
@router.get("/cases/{case_id}")
async def get_case(
    case_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    case_service: APICaseService = Depends(get_api_case_service),
):
    case = await case_service.get_case(case_id, current_user.organization_id)
    return CaseResponse.from_domain(case)
```

---

### Pattern 2: Write Operations (cases:write, sessions:create, evidence:upload)

**Current**:
```python
@router.post("/cases")
async def create_case(
    request: CaseCreateRequest,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    case_service: APICaseService = Depends(get_api_case_service),
):
    organization_id, user_id = auth_context
    case = await case_service.create_case(
        organization_id=organization_id,
        created_by=user_id,
        title=request.title,
        ...
    )
    return CaseResponse.from_domain(case)
```

**Corrected**:
```python
@router.post("/cases")
async def create_case(
    request: CaseCreateRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    case_service: APICaseService = Depends(get_api_case_service),
):
    case = await case_service.create_case(
        organization_id=current_user.organization_id,
        created_by=current_user.user_id,
        title=request.title,
        ...
    )
    return CaseResponse.from_domain(case)
```

---

### Pattern 3: Permission-Protected Operations (cases:delete, evidence:delete)

**Current**:
```python
@router.delete("/cases/{case_id}")
async def delete_case(
    case_id: str,
    auth_context: tuple[str, str] = Depends(get_auth_context),
    current_user: AuthenticatedUser = Depends(get_current_user),  # DUPLICATE!
    case_service: APICaseService = Depends(get_api_case_service),
):
    organization_id, _ = auth_context
    # Permission check happens in middleware
    await case_service.delete_case(case_id, organization_id)
    return {"status": "deleted"}
```

**Problem**: Duplicate authentication (`auth_context` + `current_user`)

**Corrected**:
```python
from faultmaven.api.middleware.auth import require_permission

@router.delete("/cases/{case_id}")
async def delete_case(
    case_id: str,
    current_user: AuthenticatedUser = Depends(require_permission("cases:delete")),
    case_service: APICaseService = Depends(get_api_case_service),
):
    # Permission already checked by require_permission()
    await case_service.delete_case(case_id, current_user.organization_id)
    return {"status": "deleted"}
```

---

## Testing Changes

### Remove Legacy Header Tests

**Current** (in test files):
```python
def test_create_case_with_legacy_headers(client):
    """Test case creation with X-Organization-ID headers."""
    response = client.post(
        "/api/v1/cases",
        headers={
            "X-Organization-ID": "org-123",
            "X-User-ID": "user-456",
        },
        json={"title": "Test", "description": "Test"},
    )
    assert response.status_code == 201
```

**Delete all tests** that use `X-Organization-ID` and `X-User-ID` headers.

---

### Keep Only JWT Token Tests

**Corrected**:
```python
def test_create_case_with_jwt(client, admin_user_token):
    """Test case creation with JWT authentication."""
    response = client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {admin_user_token}"},
        json={"title": "Test", "description": "Test"},
    )
    assert response.status_code == 201

def test_create_case_without_auth_returns_401(client):
    """Test case creation without authentication fails."""
    response = client.post(
        "/api/v1/cases",
        json={"title": "Test", "description": "Test"},
    )
    assert response.status_code == 401
```

---

## Documentation Updates

### OpenAPI Documentation

**Current** (confusing):
```yaml
paths:
  /api/v1/cases:
    post:
      security:
        - BearerAuth: []  # JWT (preferred)
        - {}             # Or legacy headers
      parameters:
        - name: X-Organization-ID
          in: header
          schema:
            type: string
          deprecated: true
```

**Corrected** (clean):
```yaml
paths:
  /api/v1/cases:
    post:
      security:
        - BearerAuth: []  # JWT required
```

---

## Acceptance Criteria

### Functional Requirements

1. ✅ **All API routes use `get_current_user()`** or `require_permission()`
2. ✅ **No `get_auth_context()` helper functions** remain in codebase
3. ✅ **No X-Organization-ID or X-User-ID headers** accepted
4. ✅ **All endpoints require JWT Bearer token** (except public auth endpoints)
5. ✅ **401 Unauthorized** when no JWT token provided
6. ✅ **403 Forbidden** when JWT token lacks required permission

### Testing Requirements

1. ✅ **All legacy header tests removed**
2. ✅ **All tests use JWT tokens** for authentication
3. ✅ **Test coverage maintained** at 90%+
4. ✅ **No regression** in existing functionality

### Code Quality

1. ✅ Simpler authentication pattern (single path)
2. ✅ No duplicate authentication dependencies
3. ✅ Clear error messages (JWT required)
4. ✅ Comprehensive docstrings
5. ✅ Type hints on all public methods

---

## Deliverables

1. **Code Files** (Update):
   - `faultmaven/api/routes/cases.py` - Remove `get_auth_context()`, use `get_current_user()`
   - `faultmaven/api/routes/sessions.py` - Remove `get_auth_context()`, use `get_current_user()`
   - `faultmaven/api/routes/evidence.py` - Remove `get_auth_context()`, use `get_current_user()`
   - `faultmaven/api/routes/agent.py` - Remove `get_auth_context()`, use `get_current_user()`

2. **Test Files** (Update):
   - Remove all tests using `X-Organization-ID` and `X-User-ID` headers
   - Update all tests to use JWT tokens only
   - Ensure 90%+ coverage maintained

3. **Documentation**:
   - OpenAPI docs automatically updated (FastAPI)
   - Remove references to "legacy headers" in comments

4. **Pull Request**:
   - Title: "TASK-020: Remove Legacy Header Authentication (Technical Debt Cleanup)"
   - Description: "Clean up unnecessary backwards compatibility layer"
   - Link to TASK-020.md

---

## Dependencies

### Required Services
- ✅ TASK-017: JWT authentication (get_current_user, require_permission)
- ✅ Existing API routes (cases, sessions, evidence, agent)

### External Dependencies
- FastAPI (existing)

### New Dependencies
- None

---

## Security Considerations

### Security Improvements

1. **Reduced Attack Surface**:
   - Single authentication path (JWT only)
   - No header injection attacks via X-Organization-ID
   - Simpler code = fewer bugs

2. **Clearer Authorization**:
   - JWT token contains all auth context (user, org, roles, permissions)
   - No ambiguity about which auth method is being used
   - Permission checks explicit via `require_permission()`

3. **Better Audit Trail**:
   - All requests authenticated via JWT (includes user_id, org_id)
   - Token revocation works (can't bypass via headers)

---

## Non-Goals (Out of Scope)

1. ❌ **API key authentication** - JWT tokens only
2. ❌ **OAuth2 flows** - JWT tokens via POST /auth/login
3. ❌ **Session-based auth** - Stateless JWT only
4. ❌ **Custom authentication schemes** - Bearer tokens only

---

## Success Criteria

**APPROVED if**:
- ✅ All `get_auth_context()` functions removed
- ✅ All API routes use `get_current_user()` or `require_permission()`
- ✅ No X-Organization-ID or X-User-ID header support
- ✅ All legacy header tests removed
- ✅ All tests use JWT tokens only
- ✅ Test coverage 90%+ maintained
- ✅ No regression in functionality

**REQUEST CHANGES if**:
- ❌ `get_auth_context()` still present
- ❌ Legacy headers still accepted
- ❌ Tests still using X-Organization-ID/X-User-ID headers
- ❌ Test coverage below 90%
- ❌ Functionality regression

---

## Design References

- **TASK-017**: JWT Authentication & Authorization Middleware
- **TASK-019**: Admin User Management (clean JWT-only pattern)
- **Principle**: YAGNI (You Aren't Gonna Need It) - Don't build backwards compatibility for non-existent legacy clients

---

## Notes

- This task removes technical debt created in TASK-016/TASK-017
- No legacy clients exist to break (greenfield system)
- Cleaner codebase from day one
- Easier to maintain going forward
- Smaller attack surface = more secure

---

**Estimated Effort**: 2-3 hours (1-2 hours implementation + 1 hour testing)
**Test-Engineer Review**: Required (ensure no regression, 90%+ coverage maintained)
