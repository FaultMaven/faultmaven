# TASK-022: Multi-Tenant End-to-End Integration Testing

## Task Metadata
- **Phase**: Week 7, Day 4 (Integration & Validation)
- **Priority**: P0 (Production readiness validation)
- **Estimated Time**: 4-6 hours
- **Dependencies**: TASK-021 (Organization Management API - PR #23)
- **Assignee**: Test-Engineer
- **Reports To**: Solutions Architect

## Objective

**Implement comprehensive end-to-end integration tests** to validate the complete multi-tenant authentication and authorization flow:

1. **User registration → Organization creation → Case management** workflow
2. **Multi-user collaboration** within organizations
3. **Cross-organization isolation** verification
4. **Role-based permissions** end-to-end testing
5. **JWT token lifecycle** (issue, refresh, revoke)
6. **Real-world scenarios** (team onboarding, case collaboration, access control)

---

## Context

### Current State

✅ **Components Implemented**:
- JWT Authentication (TASK-017)
- User Management (TASK-018)
- Admin User Management (TASK-019)
- Organization Management (TASK-021)
- Case/Session/Evidence APIs with multi-tenant support

❌ **Missing**:
- End-to-end integration tests spanning multiple components
- Real-world workflow validation
- Multi-user collaboration scenarios
- Performance testing under multi-tenant load

### Why This Matters

Individual components have been tested in isolation, but we need to verify:
- **Complete user journeys** work end-to-end
- **Multi-tenant isolation** holds under realistic scenarios
- **Role-based permissions** cascade correctly across all resources
- **Token lifecycle** works correctly (login → refresh → revoke → re-login)
- **Concurrent multi-user** operations don't violate isolation

### Evolution Path
```
TASK-017: JWT Authentication ✅
TASK-018: User Management Service ✅
TASK-019: Admin User Management ✅
TASK-021: Organization Management API ✅
TASK-022: E2E Integration Testing ← YOU ARE HERE
TASK-023: Production Deployment Readiness (next)
```

---

## Test Scenarios

### Scenario 1: New User Onboarding Flow

**User Story**: Alice registers, creates an organization, and starts her first case.

**Steps**:
1. POST /auth/register (email: alice@acme.com, password)
2. POST /auth/login (get JWT tokens)
3. POST /organizations (create "Acme Corp")
4. GET /organizations (verify user is owner)
5. POST /cases (create first troubleshooting case)
6. GET /cases (verify case visible to Alice)
7. POST /sessions (start investigation session)
8. POST /evidence (upload evidence file)

**Verifications**:
- ✅ User created successfully
- ✅ JWT token issued and valid
- ✅ Organization created with user as owner
- ✅ Case belongs to organization
- ✅ All resources properly linked (org → case → session → evidence)

---

### Scenario 2: Multi-User Collaboration

**User Story**: Bob joins Alice's organization and collaborates on a case.

**Steps**:
1. POST /auth/register (bob@acme.com)
2. POST /auth/login (Bob gets token)
3. **Alice**: POST /organizations/{org_id}/members (invite Bob as member)
4. **Bob**: GET /organizations (verify membership in "Acme Corp")
5. **Bob**: GET /cases (verify can see Alice's case)
6. **Bob**: GET /cases/{case_id} (view case details)
7. **Bob**: POST /cases/{case_id}/sessions (create new session)
8. **Bob**: PATCH /cases/{case_id} (try to update - verify member can/cannot based on permissions)

**Verifications**:
- ✅ Bob can join organization
- ✅ Bob sees organization cases
- ✅ Bob can collaborate (create sessions/evidence)
- ✅ Bob's permissions enforced (cannot delete org, cannot change roles)

---

### Scenario 3: Cross-Organization Isolation

**User Story**: Charlie creates a separate organization and cannot access Alice's data.

**Steps**:
1. POST /auth/register (charlie@beta.com)
2. POST /auth/login (Charlie gets token)
3. POST /organizations (create "Beta Inc")
4. **Charlie**: GET /organizations (verify only sees "Beta Inc", not "Acme Corp")
5. **Charlie**: GET /cases (verify empty - cannot see Alice's cases)
6. **Charlie**: GET /organizations/{acme_org_id} (verify 403 Forbidden)
7. **Charlie**: GET /cases/{alice_case_id} (verify 403 Forbidden or 404)
8. **Charlie**: POST /organizations/{acme_org_id}/members (verify 403 - cannot add members to another org)

**Verifications**:
- ✅ Charlie's organization isolated from Alice's
- ✅ Cross-organization API calls blocked (403 Forbidden)
- ✅ List endpoints filter by organization
- ✅ No data leakage between organizations

---

### Scenario 4: Role-Based Permission Escalation

**User Story**: Dave is promoted from member to admin and gains new permissions.

**Steps**:
1. **Alice**: POST /organizations/{org_id}/members (invite Dave as member)
2. **Dave**: POST /auth/login (get token_v1)
3. **Dave**: POST /organizations/{org_id}/members (verify 403 - members cannot invite)
4. **Alice**: PATCH /organizations/{org_id}/members/{dave_id} (promote to admin)
5. **Dave**: POST /auth/login (get new token_v2 - token_v1 revoked)
6. **Dave**: GET /users/me (verify token_v1 returns 403 - revoked)
7. **Dave**: POST /organizations/{org_id}/members (with token_v2, verify 201 - admin can invite)
8. **Dave**: PATCH /organizations/{org_id}/settings (verify 403 - admin cannot update settings)

**Verifications**:
- ✅ Role change revokes old JWT tokens
- ✅ New login required after role change
- ✅ New permissions apply (admin can invite members)
- ✅ Limitations enforced (admin cannot update org settings)

---

### Scenario 5: Owner Transfers and Deletion

**User Story**: Alice transfers ownership to Bob, then Bob deletes the organization.

**Steps**:
1. **Alice** (owner): PATCH /organizations/{org_id}/members/{bob_id} (set role=owner)
2. **Bob**: POST /auth/login (get new token)
3. **Bob** (new owner): PATCH /organizations/{org_id} (update org name - verify success)
4. **Alice** (ex-owner): PATCH /organizations/{org_id} (verify 403 - no longer owner)
5. **Bob**: DELETE /organizations/{org_id}/members/{alice_id} (remove Alice)
6. **Alice**: GET /organizations (verify "Acme Corp" no longer listed)
7. **Alice**: GET /cases (verify cannot access cases)
8. **Bob**: DELETE /organizations/{org_id} (delete organization)
9. **Bob**: GET /organizations (verify org marked deleted)

**Verifications**:
- ✅ Ownership transfer works
- ✅ Previous owner loses privileges
- ✅ New owner gains full control
- ✅ Member removal revokes access
- ✅ Organization deletion soft-deletes

---

### Scenario 6: JWT Token Lifecycle

**User Story**: Eve's JWT tokens expire, refresh, and get revoked.

**Steps**:
1. POST /auth/login (get access_token, refresh_token)
2. GET /users/me (verify access_token works)
3. **Wait for access_token expiry** (simulate with time manipulation if possible)
4. GET /users/me (verify 401 Unauthorized - token expired)
5. POST /auth/refresh (use refresh_token, get new access_token)
6. GET /users/me (verify new access_token works)
7. POST /auth/password/change (change password)
8. GET /users/me (verify both tokens revoked - 403)
9. POST /auth/login (new login required, get fresh tokens)

**Verifications**:
- ✅ Access tokens expire correctly
- ✅ Refresh tokens work
- ✅ Password change revokes all tokens
- ✅ Re-login works after revocation

---

### Scenario 7: Plan Tier Limit Enforcement

**User Story**: Frank's free-tier organization hits member limit.

**Steps**:
1. POST /organizations (create "Free Org", plan_tier=free)
2. **Loop 5 times**: POST /organizations/{org_id}/members (invite user1-5)
3. Verify all 5 members added successfully (Free plan: 5 members max)
4. POST /organizations/{org_id}/members (invite user6, verify 403 - limit reached)
5. GET /organizations/{org_id}/settings (verify max_members=5, current_count=6 including owner)
6. **Admin**: PATCH /organizations/{org_id}/settings (upgrade plan_tier=pro)
7. POST /organizations/{org_id}/members (invite user6, verify 201 - now allowed)

**Verifications**:
- ✅ Free plan limited to 5 members (+ owner = 6 total)
- ✅ Member limit enforced (403 when exceeded)
- ✅ Plan upgrade increases limit
- ✅ Pro plan allows 50 members

---

### Scenario 8: Concurrent Multi-User Operations

**User Story**: Multiple users create cases simultaneously within same organization.

**Steps**:
1. Create organization with 10 members
2. **Concurrently** (10 threads): Each user POST /cases (create case)
3. Verify all 10 cases created successfully
4. Verify all cases have unique case_ids
5. Verify all cases belong to same organization
6. GET /cases (verify all 10 cases visible to all members)

**Verifications**:
- ✅ Concurrent operations don't conflict
- ✅ No race conditions in case creation
- ✅ Organization_id correctly set for all cases
- ✅ All members see all cases

---

### Scenario 9: Admin User Management Across Organizations

**User Story**: System admin manages users across multiple organizations.

**Steps**:
1. Create platform admin user (is_admin=true)
2. **Admin**: GET /admin/users (list all platform users)
3. **Admin**: POST /admin/users/{user_id}/deactivate (deactivate user in org1)
4. **Deactivated user**: POST /auth/login (verify 403 - account deactivated)
5. **Admin**: POST /admin/users/{user_id}/activate (reactivate)
6. **User**: POST /auth/login (verify success)
7. **Admin**: PATCH /admin/users/{user_id}/roles (change role to admin)
8. **User**: POST /auth/login (get new token with admin role)

**Verifications**:
- ✅ Platform admin can manage all users
- ✅ Deactivation prevents login
- ✅ Reactivation restores access
- ✅ Platform admin role changes work
- ✅ Token revocation on role changes

---

### Scenario 10: Evidence-Centric Troubleshooting Workflow

**User Story**: Georgia investigates an incident using the complete evidence → case → session flow.

**Steps**:
1. POST /cases (create "Production Outage" case)
2. POST /sessions (start investigation session)
3. POST /evidence (upload server.log)
4. POST /evidence (upload metrics.png)
5. POST /evidence (upload error_trace.txt)
6. POST /evidence/{log_id}/set-primary (mark server.log as primary)
7. GET /sessions/{session_id} (verify all evidence linked)
8. GET /cases/{case_id}/statistics (verify evidence count)
9. POST /agent/execute (trigger AI analysis on primary evidence)
10. GET /sessions/{session_id} (verify agent execution results)

**Verifications**:
- ✅ Complete evidence workflow
- ✅ Evidence properly linked to session and case
- ✅ Primary evidence marking works
- ✅ AI agent can access evidence
- ✅ All resources belong to same organization

---

## Implementation Plan

### Step 1: Create Test Infrastructure

**File**: `tests/integration/test_e2e_multi_tenant_workflows.py`

```python
import pytest
from httpx import AsyncClient
from typing import Dict, Any, List

@pytest.fixture
async def test_app():
    """Create test application with real dependencies."""
    # Use actual database (not mocks) for E2E tests
    # Create isolated test database
    # Apply migrations
    # Return test client

@pytest.fixture
async def registered_user():
    """Create and return registered user with JWT token."""
    # Register user
    # Login
    # Return {user_id, email, access_token, refresh_token}

@pytest.fixture
async def organization_with_members():
    """Create organization with owner + 3 members."""
    # Create org
    # Add 3 members (owner, admin, member)
    # Return {org_id, owner_token, admin_token, member_token}

class TestUserOnboarding:
    """Test complete user onboarding flow."""

    async def test_new_user_registration_to_first_case(self, test_app):
        """Scenario 1: New user registration → org creation → first case."""
        # Implementation

class TestMultiUserCollaboration:
    """Test multi-user collaboration within organizations."""

    async def test_member_joins_and_collaborates(self, organization_with_members):
        """Scenario 2: Member joins organization and collaborates."""
        # Implementation

class TestCrossOrganizationIsolation:
    """Test cross-organization data isolation."""

    async def test_separate_orgs_cannot_access_data(self, test_app):
        """Scenario 3: Charlie cannot access Alice's organization."""
        # Implementation

# ... 7 more test classes for scenarios 4-10
```

### Step 2: Implement Each Scenario

Each test scenario becomes a comprehensive integration test that:
1. Sets up realistic multi-user environment
2. Executes complete user workflows
3. Verifies all security and isolation guarantees
4. Cleans up test data

### Step 3: Performance Baseline

Add performance assertions to E2E tests:
```python
async def test_concurrent_case_creation_performance(self):
    """Verify 10 concurrent case creations complete in <5 seconds."""
    start = time.time()

    # Create 10 cases concurrently
    tasks = [create_case(client, org_id) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    elapsed = time.time() - start
    assert elapsed < 5.0, f"Concurrent operations too slow: {elapsed}s"
    assert all(r.status_code == 201 for r in results)
```

---

## Testing Requirements

### 1. E2E Workflow Tests (tests/integration/test_e2e_multi_tenant_workflows.py)

**Test Coverage** (40-50 tests):

#### User Onboarding (5-7 tests)
- Complete registration → login → org creation → first case
- Email verification flow (if implemented)
- Password reset → re-login workflow
- First-time user experience (no organization yet)

#### Multi-User Collaboration (8-10 tests)
- Member invitation and acceptance
- Role-based collaboration (owner, admin, member)
- Concurrent operations by multiple users
- Case assignment and reassignment

#### Cross-Organization Isolation (10-12 tests)
- Separate organizations cannot access each other's data
- List endpoints filtered by organization
- Direct resource access blocked (403/404)
- Member invitations cannot cross organizations

#### Role Transitions (6-8 tests)
- Member → Admin promotion
- Admin → Member demotion
- Owner → Member (ownership transfer)
- Token revocation on role changes

#### JWT Token Lifecycle (6-8 tests)
- Token issuance on login
- Token expiry and refresh
- Token revocation (password change, role change, deactivation)
- Re-login after revocation

#### Plan Tier Limits (4-6 tests)
- Free plan: 5 member limit
- Pro plan: 50 member limit
- Enterprise: unlimited
- Plan upgrades/downgrades

#### Concurrent Operations (4-6 tests)
- Multiple users creating cases simultaneously
- No race conditions in ID generation
- Organization_id correctly set under load
- Database transaction isolation

**Expected Tests**: 40-50 tests

---

### 2. Performance Baseline Tests (tests/integration/test_e2e_performance.py)

**Test Coverage** (10-15 tests):

#### Response Time Baselines
- Login: < 200ms
- Organization creation: < 300ms
- Case creation: < 400ms
- List operations: < 500ms (100 items)

#### Concurrent Load
- 10 concurrent case creations: < 5s
- 20 concurrent logins: < 3s
- 50 concurrent read operations: < 2s

#### Database Query Optimization
- Case list query: < 50ms (no N+1 queries)
- Organization member list: < 30ms
- Evidence list query: < 40ms

**Expected Tests**: 10-15 tests

---

### 3. Real-World Scenario Tests (tests/integration/test_e2e_real_world_scenarios.py)

**Test Coverage** (15-20 tests):

#### Team Onboarding Scenario
- Manager creates organization
- Invites 5 team members
- Each member creates first case
- Team collaborates on shared investigation

#### Incident Response Scenario
- Incident detected
- Case created with severity=critical
- Evidence uploaded (logs, metrics, traces)
- Multiple sessions created by different engineers
- AI agent analyzes evidence
- Case closed with resolution

#### Multi-Org Platform Usage
- 3 separate organizations
- Each org has 5-10 members
- Each org creates 10 cases
- Verify complete isolation
- Verify no performance degradation

**Expected Tests**: 15-20 tests

---

## Acceptance Criteria

### Functional Requirements

1. ✅ **10 E2E workflow scenarios** implemented and passing
2. ✅ **Complete user journeys** tested end-to-end
3. ✅ **Multi-tenant isolation** verified under realistic loads
4. ✅ **Role-based permissions** cascading correctly
5. ✅ **JWT token lifecycle** working (issue, refresh, revoke)
6. ✅ **Plan tier limits** enforced correctly
7. ✅ **Concurrent operations** safe (no race conditions)
8. ✅ **Performance baselines** met (response times < thresholds)

### Testing Requirements

1. ✅ **E2E workflow tests**: 40-50 tests covering 10 scenarios
2. ✅ **Performance tests**: 10-15 tests with baseline assertions
3. ✅ **Real-world scenarios**: 15-20 tests simulating actual usage
4. ✅ **Total**: 65-85 comprehensive E2E tests
5. ✅ **All tests pass** consistently (no flaky tests)
6. ✅ **Test execution time**: < 5 minutes for full E2E suite

### Code Quality

1. ✅ Realistic test data (actual user workflows)
2. ✅ Proper test isolation (each test independent)
3. ✅ Comprehensive assertions (verify all security guarantees)
4. ✅ Clear test names describing scenarios
5. ✅ Proper cleanup (no test data leakage)
6. ✅ Performance assertions (response time thresholds)

---

## Deliverables

1. **Test Files** (New):
   - `tests/integration/test_e2e_multi_tenant_workflows.py` - Main E2E workflow tests (40-50 tests)
   - `tests/integration/test_e2e_performance.py` - Performance baseline tests (10-15 tests)
   - `tests/integration/test_e2e_real_world_scenarios.py` - Real-world scenario tests (15-20 tests)
   - `tests/integration/conftest.py` - Shared E2E test fixtures

2. **Test Infrastructure**:
   - E2E test database setup/teardown
   - Multi-user test fixtures
   - Performance timing utilities
   - Test data generators

3. **Documentation**:
   - E2E test execution guide
   - Performance baseline documentation
   - Known limitations and edge cases

4. **Pull Request**:
   - Title: "test: implement multi-tenant E2E integration tests (TASK-022)"
   - Description: Comprehensive E2E validation of multi-tenant auth flow
   - Link to TASK-022.md
   - Test execution report showing all scenarios passing

---

## Dependencies

### Required Services
- ✅ All authentication services (TASK-017, TASK-018, TASK-019)
- ✅ Organization management (TASK-021)
- ✅ Case/Session/Evidence APIs (TASK-011, TASK-012, TASK-013)
- ✅ Database with migrations applied

### External Dependencies
- pytest-asyncio (existing)
- httpx (existing)
- asyncio (Python stdlib)

### New Dependencies
- None (all existing)

---

## Success Criteria

**APPROVED if:**
- ✅ All 10 E2E workflow scenarios implemented (65-85 tests)
- ✅ Complete user journeys tested end-to-end
- ✅ Multi-tenant isolation verified (no cross-org leaks)
- ✅ Role-based permissions cascading correctly
- ✅ JWT token lifecycle working (revocation, refresh)
- ✅ Plan tier limits enforced
- ✅ Concurrent operations safe (no race conditions)
- ✅ Performance baselines met (response times acceptable)
- ✅ All tests pass consistently
- ✅ Test execution time < 5 minutes

**REQUEST CHANGES if:**
- ❌ Missing E2E scenarios (less than 10)
- ❌ Cross-organization data leaks found
- ❌ Role permission cascading failures
- ❌ JWT token lifecycle issues
- ❌ Race conditions in concurrent operations
- ❌ Performance below baselines
- ❌ Flaky tests (inconsistent failures)

---

## Non-Goals (Out of Scope)

1. ❌ **Load testing** - Separate task (TASK-023)
2. ❌ **Security penetration testing** - Separate security audit task
3. ❌ **UI/Browser E2E tests** - Backend API only
4. ❌ **External integrations** - Focus on core platform
5. ❌ **Billing/payment workflows** - Future enhancement
6. ❌ **Email delivery testing** - Mock email service

---

## Notes

- E2E tests use real database (not mocks) to validate full integration
- Tests should be repeatable and independent
- Performance baselines establish acceptable response times
- These tests validate the entire multi-tenant architecture works correctly
- Critical for production readiness

---

**Estimated Effort**: 4-6 hours (2-3 hours implementation + 2-3 hours debugging/optimization)
**Assignee**: Test-Engineer (specializes in comprehensive test scenarios)
**Complexity**: High (requires realistic multi-user orchestration)
