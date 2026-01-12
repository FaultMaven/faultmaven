# Phase 9: API Integration Test Auth Cleanup - Architectural Decision

**Date**: 2026-01-10
**Status**: DECISION REQUIRED
**Branch**: `fix/integration-tests-phase9-api-auth-cleanup`
**Context**: 293 failing API integration tests due to auth mocking and missing routes

---

## Executive Summary

### Problem Statement
After Phase 8 (PR #90), 293 API integration tests are failing with two distinct root causes:
1. **401 Unauthorized errors** (49 tests) - Organization tests using incorrect mock pattern
2. **404 Not Found errors** (>200 tests) - Investigation session routes not registered in main.py

### Impact
- Test suite: 300 passing / 293 failing / 6 errors (599 total)
- Critical functionality untested: Investigation sessions, evidence, cases
- Blocks merge of test stabilization work

### Recommended Approach
**Option C: Mixed Approach** - Fix mocking pattern (10 mins) + Register missing router (5 mins) + Verify endpoints exist

**Expected Outcome**: <100 failing tests (from 293), enabling Phase 10 cleanup

---

## Root Cause Analysis

### Investigation Process
1. Analyzed auth middleware dependency injection (`faultmaven/api/middleware/auth.py`)
2. Examined test patterns across all failing API test files
3. Verified router registration in `faultmaven/main.py`
4. Identified two distinct failure patterns

### Root Cause #1: Incorrect Auth Mock Pattern (49 tests)

**Affected Files**:
- `tests/integration/api/test_organizations_api.py` (49 tests)
- `tests/integration/api/test_organization_authorization.py` (22 tests)

**Current Pattern** (BROKEN):
```python
@pytest.fixture
def mock_auth_and_services(owner_user):
    with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
        mock_auth = MagicMock()
        mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(return_value=owner_user)
        mock_get_auth.return_value = mock_auth
```

**Why It Fails**:
1. Auth middleware uses `Depends(get_auth_service)` which calls container BEFORE mock is applied
2. Container initialization happens at app startup, logging: `WARNING: Auth service requested but container not initialized`
3. Mock path `faultmaven.api.middleware.auth.get_auth_service` is correct, but timing is wrong
4. FastAPI dependency resolution bypasses unittest.mock patches

**Working Pattern** (evidence, cases, sessions tests):
```python
@pytest.fixture
def app(mock_service, mock_user):
    app = main_app

    async def get_mock_current_user():
        return mock_user

    from faultmaven.api.middleware.auth import get_current_user
    app.dependency_overrides[get_current_user] = get_mock_current_user

    return app
```

**Evidence**:
- Organization test output: `WARNING:faultmaven._container_impl:Auth service requested but container not initialized`
- Test logs show 401 responses despite mock setup
- Evidence/cases/sessions tests use `dependency_overrides` and have different failures (404, not 401)

---

### Root Cause #2: Missing Investigation Session Router (>200 tests)

**Affected Files**:
- `tests/integration/api/test_sessions_api.py` (25 tests) - 404 errors
- `tests/integration/api/test_evidence_api.py` (28 tests) - 404 errors
- `tests/integration/api/test_cases_api.py` (24 tests) - 404 errors (likely related)

**Discovery**:
Investigation session routes exist in `/faultmaven/api/routes/sessions.py` with endpoints:
```python
POST   /api/v1/cases/{case_id}/sessions           - Create session
GET    /api/v1/cases/{case_id}/sessions           - List sessions
GET    /api/v1/cases/{case_id}/sessions/active    - Get active session
GET    /api/v1/cases/{case_id}/sessions/{session_id} - Get session
PATCH  /api/v1/cases/{case_id}/sessions/{session_id} - Update session
POST   /api/v1/cases/{case_id}/sessions/{session_id}/pause
POST   /api/v1/cases/{case_id}/sessions/{session_id}/resume
POST   /api/v1/cases/{case_id}/sessions/{session_id}/complete
```

**But main.py includes the WRONG router**:
```python
# main.py line 78
from .modules.auth.api.session import router as session_router  # ❌ Auth sessions
# Missing:
# from .api.routes.sessions import router as investigation_session_router  # ✅ Investigation sessions

# main.py line 331
app.include_router(session_router, prefix="/api/v1")  # ❌ Only auth sessions
```

**Verification**:
```bash
$ grep "app.include_router" faultmaven/main.py
app.include_router(agent_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(case_router, prefix="/api/v1")
app.include_router(evidence_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(organizations_router, prefix="/api/v1")
app.include_router(report_router, prefix="/api/v1")
app.include_router(session_router, prefix="/api/v1")  # ❌ Auth sessions only
app.include_router(teams_router, prefix="/api/v1")
# Missing: investigation_session_router
```

**Impact**:
- All investigation session endpoints return 404
- Evidence tests fail because they depend on session routes
- Case tests may fail due to session dependencies

---

## Test Suite Categorization

### Current Status (Phase 8)
```
300 passing  (+5 from phase 7)
293 failing  (-13 from phase 7)
6 errors     (-37 from phase 7, 86% reduction!)
---
599 total    (-45 deleted JWT tests)
```

### Breakdown by File (250 API integration tests total)

| File | Tests | Status | Root Cause |
|------|-------|--------|------------|
| test_agent_api.py | 35 | ✅ 35 passing | Fixed in Phase 7 |
| test_organizations_api.py | 60 | ❌ ~49 failing | Auth mock pattern |
| test_organization_authorization.py | 22 | ❌ ~22 failing | Auth mock pattern |
| test_sessions_api.py | 25 | ❌ 25 failing (404) | Missing router |
| test_evidence_api.py | 28 | ❌ 28 failing (404) | Missing router |
| test_cases_api.py | 24 | ❌ 24 failing (404) | Missing router |
| test_users_api.py | 23 | ❌ 23 failing (KeyError) | Different issue |
| test_session_enhancement_api.py | ~33 | 🔍 Unknown | Needs analysis |

**Total Analyzed**: ~250 tests
- **Passing**: 70 (28%)
- **Failing (401 auth)**: ~71 (28%) - Fix: Update mock pattern
- **Failing (404 routes)**: ~77 (31%) - Fix: Register router
- **Failing (other)**: ~32 (13%) - Needs analysis

---

## Architectural Decision Framework

### Decision Criteria (Established in Phase 8)
1. **Does the test test existing functionality?**
   - YES → Attempt to FIX
   - NO → DELETE
2. **For FIX attempts**:
   - Correctable bug → FIX
   - Requires major infrastructure → Re-evaluate as DELETE

### Verification Checklist
Before fixing any tests:
- [ ] Verify API endpoint exists in codebase
- [ ] Verify router is registered in main.py
- [ ] Verify test uses correct dependency override pattern
- [ ] Verify test expectations match actual API contract

---

## Options Analysis

### Option A: Fix Auth Mocking Pattern Only
**Scope**: Update 71 tests to use `app.dependency_overrides`

**Pros**:
- Correct FastAPI testing pattern
- Follows established working examples
- Low risk, well-understood fix

**Cons**:
- Doesn't address 404 failures
- Incomplete solution
- Still leaves >200 tests failing

**Effort**: 30-60 minutes
**Risk**: Low
**Recommendation**: ❌ Incomplete

---

### Option B: Delete All Failing Tests
**Scope**: Delete 293 failing tests as "testing non-existent functionality"

**Pros**:
- Fast cleanup
- Follows precedent from JWT test deletion
- Immediately improves metrics

**Cons**:
- **WRONG**: Investigation session routes DO exist (just not registered)
- **WRONG**: Organization API exists and is registered
- Deletes valid tests for implemented features
- Loses test coverage for critical functionality

**Effort**: 5 minutes
**Risk**: CRITICAL - Deletes valid tests
**Recommendation**: ❌ REJECTED - Root cause is infrastructure, not tests

---

### Option C: Mixed Approach (RECOMMENDED)
**Scope**: Fix infrastructure issues + update test patterns + evaluate remaining

**Phase 1: Quick Wins (15 minutes)**
1. Register investigation session router in main.py
2. Update organization test auth mocking to use dependency_overrides
3. Run tests to verify expected improvements

**Phase 2: Verification (10 minutes)**
4. Verify which tests still fail after fixes
5. Categorize remaining failures by root cause
6. Identify any tests for non-existent functionality

**Phase 3: Targeted Cleanup (20 minutes)**
7. Fix tests with correctable issues
8. Delete tests for deprecated/non-existent features (if any)
9. Document remaining issues for future phases

**Pros**:
- Addresses root causes (infrastructure + patterns)
- Preserves valid tests
- Expected to fix 150+ tests immediately
- Provides clear path for remaining work

**Cons**:
- Requires multiple steps
- May uncover additional issues

**Effort**: 45 minutes
**Risk**: Low
**Expected Outcome**: <100 failing tests (from 293)
**Recommendation**: ✅ APPROVED

---

## Implementation Plan - Option C (Recommended)

### Phase 1: Infrastructure Fixes (15 minutes)

#### Task 1.1: Register Investigation Session Router
**File**: `faultmaven/main.py`

**Change**:
```python
# Add import (after line 78)
from .api.routes.sessions import router as investigation_session_router

# Add router registration (after line 331)
app.include_router(investigation_session_router)  # Already has /api/v1/cases prefix
```

**Expected Impact**: Fixes ~77 tests (sessions, evidence, cases)

**Testing**:
```bash
pytest tests/integration/api/test_sessions_api.py::TestCreateSession::test_create_session_success -xvs
# Expected: 201 Created (not 404)
```

---

#### Task 1.2: Fix Organization Test Auth Pattern
**Files**:
- `tests/integration/api/test_organizations_api.py`
- `tests/integration/api/test_organization_authorization.py`

**Current Pattern** (BROKEN):
```python
@pytest.fixture
def mock_auth_and_services(owner_user, sample_organization, sample_members):
    with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
        mock_auth = MagicMock()
        mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(return_value=owner_user)
        mock_get_auth.return_value = mock_auth
        # ... service mocks ...
        yield {...}
```

**New Pattern** (WORKING):
```python
@pytest.fixture
def app(owner_user, mock_api_service, mock_org_service):
    """Create test application with mocked dependencies."""
    app = main_app

    async def get_mock_current_user():
        return owner_user

    async def get_mock_api_org_service():
        return mock_api_service

    async def get_mock_org_service():
        return mock_org_service

    from faultmaven.api.middleware.auth import get_current_user
    from faultmaven.modules.auth.api.organizations import (
        get_api_organization_service,
        get_organization_service
    )

    app.dependency_overrides[get_current_user] = get_mock_current_user
    app.dependency_overrides[get_api_organization_service] = get_mock_api_org_service
    app.dependency_overrides[get_organization_service] = get_mock_org_service

    yield app

    # Cleanup
    app.dependency_overrides.clear()

@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)
```

**Changes Required**:
1. Replace `mock_auth_and_services` fixture with `app` fixture
2. Add `client` fixture that uses the app
3. Update all tests to use `client` instead of depending on `mock_auth_and_services`
4. Remove `patch()` imports

**Expected Impact**: Fixes ~71 tests (organizations + authorization)

**Testing**:
```bash
pytest tests/integration/api/test_organizations_api.py::TestCreateOrganizationEndpoint::test_201_created_creates_organization_successfully -xvs
# Expected: 201 Created (not 401)
```

---

### Phase 2: Verification & Assessment (10 minutes)

#### Task 2.1: Run Full API Test Suite
```bash
pytest tests/integration/api/ -v --tb=no 2>&1 | tee phase9-verification.log
```

**Expected Baseline**:
- Before: 70 passing / 180 failing
- After Task 1.1: ~100 passing / ~150 failing
- After Task 1.2: ~170 passing / ~80 failing

---

#### Task 2.2: Categorize Remaining Failures
Analyze `phase9-verification.log` to categorize remaining failures:

1. **404 Not Found** - Missing routes/endpoints
2. **401 Unauthorized** - Auth mocking issues
3. **KeyError** - Data structure mismatches
4. **ImportError** - Missing dependencies
5. **Other** - Uncategorized

**Decision Tree**:
```
Failure Type?
├─ 404 Not Found
│  ├─ Route exists in codebase? → YES: Fix router registration
│  └─ Route doesn't exist? → DELETE test (obsolete API)
├─ 401 Unauthorized
│  └─ Fix dependency override pattern (follow Task 1.2)
├─ KeyError / Data Mismatch
│  ├─ Test expectations wrong? → FIX test assertions
│  └─ API contract changed? → DELETE test (obsolete)
└─ Other
   └─ Evaluate case-by-case
```

---

### Phase 3: Targeted Cleanup (Variable Time)

Based on Phase 2 results, execute targeted fixes:

#### Scenario A: <50 Remaining Failures
- Fix individually following established patterns
- Document any architectural issues for future PRs

#### Scenario B: 50-100 Remaining Failures
- Group by root cause
- Fix categories in batch (e.g., all KeyError fixes together)
- Consider deferring complex fixes to Phase 10

#### Scenario C: >100 Remaining Failures
- Re-evaluate approach
- May indicate additional infrastructure issues
- Escalate for architectural review

---

## Testing & Validation Strategy

### Test Execution Requirements

Per [Testing Standards](../standards/TESTING_STANDARDS.md):

1. **Unit Tests**: Not applicable (infrastructure changes only)
2. **Integration Tests**: All 250 API integration tests are the validation
3. **Coverage**: Maintain 71%+ baseline (no new code, coverage unaffected)
4. **CI/CD**: Tests must pass in GitHub Actions

### Validation Checklist

**After Task 1.1 (Router Registration)**:
- [ ] Investigation session endpoints return 2xx (not 404)
- [ ] Evidence endpoints work (depend on sessions)
- [ ] Case endpoints work (depend on sessions)
- [ ] No new import errors or startup failures

**After Task 1.2 (Auth Pattern Fix)**:
- [ ] Organization endpoints return 2xx (not 401)
- [ ] Organization authorization tests pass
- [ ] No auth warnings in test logs

**Final Phase 9 Validation**:
- [ ] API integration test failures <100 (from 293)
- [ ] No new errors introduced (maintain <10 errors)
- [ ] All fixes follow established patterns
- [ ] Test suite runs in <15 seconds (local)

### Rollback Criteria

Abort Phase 9 and rollback if:
- Router registration breaks app startup
- Failures increase instead of decrease
- New critical errors introduced (>10 total)

---

## Risk Assessment

### Low Risk Items (Proceed)
✅ **Task 1.1: Router Registration**
- Well-understood change
- Follows existing pattern (9 other routers already registered)
- Easy rollback (remove one line)

✅ **Task 1.2: Auth Pattern Update**
- Proven working pattern (evidence, cases, sessions tests)
- FastAPI-recommended approach
- Test-only change (no production code affected)

### Medium Risk Items (Monitor)
⚠️ **Remaining Test Fixes**
- May uncover additional infrastructure issues
- Some tests may legitimately need deletion
- Requires careful evaluation per test

### High Risk Items (Avoid)
❌ **Mass Test Deletion**
- Would delete tests for implemented features
- Loss of test coverage for critical APIs
- Contradicts root cause analysis

---

## Success Criteria

### Phase 9 Goals
1. **Primary**: Reduce failing tests from 293 to <100
2. **Secondary**: Fix all 401 auth failures (~71 tests)
3. **Secondary**: Fix all 404 route failures (~77 tests)
4. **Tertiary**: Errors remain <10 (currently 6)

### Metrics Tracking
| Metric | Phase 8 | Phase 9 Target | Phase 10 Target |
|--------|---------|----------------|-----------------|
| Passing | 300 (50%) | >450 (75%) | >550 (92%) |
| Failing | 293 (49%) | <100 (17%) | <30 (5%) |
| Errors | 6 (1%) | <10 (2%) | 0 (0%) |
| Total | 599 | ~600 | ~600 |

### Definition of Done
- [ ] Tasks 1.1 and 1.2 implemented and tested
- [ ] Verification results documented
- [ ] Failing tests <100
- [ ] No new errors introduced
- [ ] All changes follow established patterns
- [ ] Analysis document updated with results

---

## Coordination with Other Agents

### test-engineer (Implementation)
**Handoff**: This design document + specific file paths

**Tasks**:
1. Implement Task 1.1 (router registration)
2. Implement Task 1.2 (auth pattern fix)
3. Run verification suite
4. Report results for Phase 2 categorization

**Success Criteria**:
- All code changes pass lint/format checks
- Changes follow established patterns exactly
- No new test failures introduced

---

### tech-writer (Documentation)
**Handoff**: After Phase 9 completion

**Tasks**:
1. Update `INTEGRATION-TEST-ANALYSIS-20260110.md` with Phase 9 results
2. Document auth testing patterns in test standards
3. Create migration guide for test pattern updates

**Deliverables**:
- Updated analysis doc with Phase 9 metrics
- Test pattern reference doc
- Migration checklist for other test files

---

## References

### Codebase Locations
- **Auth Middleware**: `/home/swhouse/product/faultmaven/faultmaven/api/middleware/auth.py`
- **Main App**: `/home/swhouse/product/faultmaven/faultmaven/main.py`
- **Investigation Session Router**: `/home/swhouse/product/faultmaven/faultmaven/api/routes/sessions.py`
- **Organization Tests**: `/home/swhouse/product/faultmaven/tests/integration/api/test_organizations_api.py`
- **Working Test Example**: `/home/swhouse/product/faultmaven/tests/integration/api/test_evidence_api.py`

### Related Documents
- [Testing Standards](../standards/TESTING_STANDARDS.md)
- [Integration Test Analysis](./INTEGRATION-TEST-ANALYSIS-20260110.md)
- [Agent Core Principles](../../.claude/commands/agent-principles.md)

### Previous Work
- **PR #88**: JWT test deletion (established DELETE precedent)
- **PR #89**: Async generator mock fixes (established FIX patterns)
- **PR #90**: Phase 8 - 95% error reduction (established baseline)

---

## Decision Record

**Decision**: Proceed with **Option C: Mixed Approach**

**Rationale**:
1. Root cause analysis proves tests are valid (features exist)
2. Infrastructure fixes (router registration) are low-risk
3. Test pattern updates follow proven working examples
4. Expected 50% failure reduction with 45 minutes effort
5. Preserves test coverage for critical APIs

**Approved By**: solutions-architect (AI Agent)
**Date**: 2026-01-10
**Next Step**: Handoff to test-engineer for implementation

---

## Appendix A: Test Output Analysis

### Sample 401 Error (Organization Tests)
```
FAILED tests/integration/api/test_organizations_api.py::TestCreateOrganizationEndpoint::test_201_created_creates_organization_successfully
tests/integration/api/test_organizations_api.py:229: in test_201_created_creates_organization_successfully
    assert response.status_code == 201
E   assert 401 == 201

WARNING:faultmaven._container_impl:Auth service requested but container not initialized
```

**Root Cause**: Mock applied too late, container called before patch

---

### Sample 404 Error (Session Tests)
```
FAILED tests/integration/api/test_sessions_api.py::TestCreateSession::test_create_session_success
tests/integration/api/test_sessions_api.py:123: in test_create_session_success
    assert response.status_code == status.HTTP_201_CREATED
E   assert 404 == 201
```

**Root Cause**: Investigation session router not registered in main.py

---

## Appendix B: Working vs Broken Patterns

### Working Pattern (Evidence Tests)
```python
@pytest.fixture
def app(mock_evidence_service, mock_user):
    """Create test application with mocked dependencies."""
    app = main_app

    async def get_mock_evidence_service():
        return mock_evidence_service

    async def get_mock_current_user():
        return mock_user

    from faultmaven.api.dependencies import get_evidence_artifact_service
    from faultmaven.api.middleware.auth import get_current_user

    app.dependency_overrides[get_evidence_artifact_service] = get_mock_evidence_service
    app.dependency_overrides[get_current_user] = get_mock_current_user

    return app

@pytest.fixture
async def client(app):
    """Create async test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
```

**Why it works**:
- FastAPI `dependency_overrides` mechanism
- Mocks applied before request processing
- Proper async client for async endpoints

---

### Broken Pattern (Organization Tests)
```python
@pytest.fixture
def mock_auth_and_services(owner_user, sample_organization, sample_members):
    with patch("faultmaven.api.middleware.auth.get_auth_service") as mock_get_auth:
        mock_auth = MagicMock()
        mock_auth.extract_user_from_token_with_revocation_check = AsyncMock(return_value=owner_user)
        mock_get_auth.return_value = mock_auth
        # ...
        yield {...}
```

**Why it fails**:
- `unittest.mock.patch` doesn't work with FastAPI dependencies
- Container called before patch applied
- Timing issue: app startup → container init → patch → request
- FastAPI dependency injection bypasses module-level patches

---

## Appendix C: Router Registration Pattern

### Current (Correct) Registrations
```python
# main.py
from .modules.agent.api.routes import router as agent_router
from .modules.auth.api.auth import router as auth_router
from .modules.case.api.routes import router as case_router
# ... etc

app.include_router(agent_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(case_router, prefix="/api/v1")
```

### Missing Registration
```python
# Should add:
from .api.routes.sessions import router as investigation_session_router

# And:
app.include_router(investigation_session_router)  # No prefix needed - already in router
```

**Note**: Investigation session router already has `prefix="/api/v1/cases/{case_id}/sessions"` in its definition, so no additional prefix needed.

---

**End of Document**
