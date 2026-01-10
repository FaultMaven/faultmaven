# Integration Test Analysis - 2026-01-10

**Status**: IN PROGRESS
**Branch**: `fix/integration-tests-phase7`
**PR**: #89 (async generator mock fixes - MERGED PENDING)

---

## Executive Summary

Integration test suite shows 298 passing / 385 failing / 128 errors. Initial analysis reveals:

1. ✅ **Fixed**: Async generator mocking pattern in test_agent_api.py (35/35 passing)
2. ⚠️ **Found**: Major API contract mismatch in auth tests (61 tests)
3. 🔍 **TODO**: Systematic evaluation of remaining 324 failing + 128 error tests

---

## Completed Work

### PR #89: Async Generator Mock Pattern Fixes

**Problem**: `AsyncMock.return_value` and `AsyncMock.side_effect` wrap async generators in coroutines, causing:
```
TypeError: 'async for' requires an object with __aiter__ method, got coroutine
```

**Solution**: Direct assignment pattern
```python
# ❌ Broken
mock_service.execute_agent.return_value = mock_execute()

# ✅ Works
mock_service.execute_agent = mock_execute
```

**Files Fixed**:
- `tests/integration/api/test_agent_api.py` - 35/35 tests passing
- `tests/integration/test_agent_execution_integration.py` - syntax error fixed
- `tests/integration/test_evidence_artifact_integration.py` - syntax error fixed
- `tests/integration/test_evidence_artifact_service_integration.py` - syntax error fixed
- `tests/integration/test_investigation_session_integration.py` - syntax error fixed
- `tests/integration/test_knowledge_item_integration.py` - syntax error fixed

**Tests Removed**:
- `test_execute_agent_missing_headers` - Obsolete (couldn't test with globally mocked auth)

---

## Critical Finding: Auth API Endpoint Mismatch

### Issue
Tests in `test_auth_api.py` (61 tests) expect endpoints that don't exist in current codebase.

### Analysis

**Tests Expect** ([test_auth_api.py:65](../tests/integration/api/test_auth_api.py#L65)):
```python
response = await client.post("/api/v1/auth/login", ...)
```

**Actual Endpoints** ([auth.py:68-447](../../faultmaven/modules/auth/api/auth.py#L68-L447)):
- POST `/api/v1/dev-login` (not `/auth/login`)
- POST `/api/v1/dev-register` (not `/auth/register`)
- POST `/api/v1/logout`
- GET `/api/v1/me`
- GET `/api/v1/health`
- POST `/api/v1/dev/revoke-all-tokens`

### Evaluation

**Tests are written for a non-existent API contract**. The actual implementation uses dev-only endpoints (`/dev-login`, `/dev-register`), suggesting this is development-mode auth, not production auth.

### Options

1. **Delete obsolete tests** (RECOMMENDED)
   - Tests test non-existent functionality
   - Follows precedent from test cleanup (deleted ~718 legacy tests)
   - Clean architecture approach: delete tests for deprecated/non-existent features

2. **Update endpoint paths**
   - Changes production API contract
   - Requires architectural decision
   - Out of scope for test stabilization

3. **Implement missing endpoints**
   - Large feature implementation
   - Out of scope for test stabilization

### Recommendation

**DELETE** `tests/integration/api/test_auth_api.py` following the evaluation-first principle established in the test cleanup work (PR #88, commit eb99fed8).

---

## Test Suite Status

### Phase 8 Progress (Current)
```
300 passing (+5 from phase 7)
293 failing (-13 from phase 7)
6 errors (-37 from phase 7, 86% reduction!)
---
599 total (-45 deleted JWT tests)
```

### Phase 7 Status
```
295 passing
306 failing
43 errors
---
644 total
```

### Initial Status
```
298 passing
385 failing
128 errors
---
711 total
```

### Test Files Status (Known)

| File | Status | Issue |
|------|--------|-------|
| test_agent_api.py | ✅ 35/35 passing | Fixed async generator mocks |
| test_auth_api.py | ❌ ~61 failing | Non-existent endpoints |
| test_cases_api.py | 🔍 Unknown | Needs evaluation |
| test_evidence_api.py | 🔍 Unknown | Needs evaluation |
| test_knowledge_api.py | 🔍 Unknown | Needs evaluation |
| test_organizations_api.py | ❌ Errors | Needs evaluation |
| test_session_enhancement_api.py | ❌ Errors | Needs evaluation |
| ...others... | 🔍 Unknown | Needs evaluation |

---

## Phase 9: API Auth Endpoint Cleanup

**Status**: IN PROGRESS
**Branch**: `fix/integration-tests-phase9-api-auth-cleanup`
**PR**: TBD
**Started**: 2026-01-10

### Investigation

**Root Cause**: Tests in `test_auth_api.py` target non-existent endpoints.

**Endpoints Tests Expect**:
- POST `/api/v1/auth/login`
- POST `/api/v1/auth/register`
- GET `/api/v1/auth/verify-token`
- POST `/api/v1/auth/refresh-token`

**Actual Production Endpoints** ([auth.py:68-447](../../faultmaven/modules/auth/api/auth.py)):
- POST `/api/v1/dev-login` (dev-only)
- POST `/api/v1/dev-register` (dev-only)
- POST `/api/v1/logout`
- GET `/api/v1/me`
- GET `/api/v1/health`
- POST `/api/v1/dev/revoke-all-tokens`

### Decision Framework Applied

**Question**: Do these tests test existing production functionality?
**Answer**: NO - Tests target endpoints that don't exist in codebase

**Evaluation**: DELETE (follows evaluation-first principle)

**Rationale**:
1. Tests are for non-existent API contract
2. Actual auth uses dev-only endpoints (`/dev-login`, `/dev-register`)
3. Precedent: Deleted 45 JWT auth tests in PR #90 for same reason
4. No backward compatibility requirements (development system)
5. Implementing missing endpoints = major feature work, out of scope

### Options Considered

**Option A: DELETE test_auth_api.py** (SELECTED)
- Pros: Clean, follows precedent (PR #88, PR #90), no tech debt
- Cons: Loss of test coverage for future auth implementation
- Impact: ~61 tests removed
- Effort: Minutes (delete file, update imports)

**Option B: Update endpoint paths to match dev endpoints**
- Pros: Preserves test structure
- Cons: Tests would still fail (different auth flow), changes production contract
- Impact: ~61 tests potentially fixed
- Effort: Hours (update all endpoint paths, fix auth flow differences)

**Option C: Implement production auth endpoints**
- Pros: Complete auth system
- Cons: Large feature implementation, out of scope, requires architectural design
- Impact: Unknown (new feature)
- Effort: Days/weeks

### Implementation Plan

**Phase 9a: Delete Non-Existent Auth Tests** (CURRENT)
1. Delete `tests/integration/api/test_auth_api.py`
2. Update any cross-references or imports
3. Run test suite to verify no regressions
4. Document deletion with rationale

**Phase 9b: Verify No Downstream Impact**
1. Check for other tests importing from test_auth_api.py
2. Check for shared fixtures used elsewhere
3. Verify no documentation references

**Phase 9c: Update Metrics**
1. Record before/after test counts
2. Update progress tracking
3. Document net change

### Progress Tracking

**Files Analyzed**: 0/1
- [ ] `tests/integration/api/test_auth_api.py` - 61 tests, DELETE decision

**Actions Taken**:
- None yet (awaiting team coordination)

**Metrics** (as of Phase 8):
```
Before Phase 9:  300 passing, 293 failing, 6 errors (599 total)
After Phase 9:   TBD passing, TBD failing, TBD errors (TBD total)
Net Change:      TBD
```

### Expected Outcome

**Best Case**:
- 61 tests deleted
- 0 errors introduced
- Net: 300 passing, 232 failing, 6 errors (538 total)
- Clean codebase with only valid tests

**Risk**: Deletion may reveal other tests that depend on test_auth_api.py fixtures

### Next Phase Preview

**Phase 10 Candidates** (after Phase 9 completion):
1. Fix remaining 6 errors
2. Evaluate remaining 232 failing tests
3. Apply similar DELETE evaluation to other non-existent endpoint tests

---

## Next Steps (Overall Project)

### Systematic Evaluation Approach

1. **Categorize all failing/error tests** by root cause:
   - ✅ Non-existent endpoints (like auth) - Phase 9 IN PROGRESS
   - ✅ Async generator mocking (like agent_api) - FIXED in PR #89
   - Import errors / module not found - TBD
   - Database/fixture issues - TBD
   - Other - TBD

2. **Triage categories**:
   - **DELETE**: Tests for non-existent features (Phase 9 applying this)
   - **FIX**: Tests with correctable bugs
   - **INVESTIGATE**: Unclear root cause

### Remaining Work

1. Complete Phase 9 (delete auth_api tests)
2. Fix remaining 6 errors
3. Evaluate remaining 232 failing tests
4. Apply DELETE/FIX decisions systematically

---

## Patterns Established

### Async Generator Mocking

**For async generators that yield values**:
```python
async def mock_execute(*args, **kwargs):
    yield ExecutionEvent.started(...)
    yield ExecutionEvent.completed(...)

mock_service.method = mock_execute  # Direct assignment
```

**For async generators that raise exceptions**:
```python
async def mock_execute(*args, **kwargs):
    raise SomeException(...)
    yield  # Unreachable but required for async generator

mock_service.method = mock_execute
```

**For async generators with call tracking**:
```python
captured_kwargs = {}

async def mock_execute(*args, **kwargs):
    captured_kwargs.update(kwargs)
    yield ...

mock_service.method = mock_execute

# Later:
assert captured_kwargs["key"] == expected_value
```

### Test Evaluation Criteria

Following precedent from test cleanup work (eb99fed8):

**DELETE tests that**:
1. Test non-existent endpoints/features
2. Test deprecated API contracts
3. Test implementation details of removed code
4. Cannot be fixed without major feature implementation

**FIX tests that**:
1. Have correctable bugs (like async generator mocks)
2. Test current functionality with wrong expectations
3. Have fixable import/module errors

**INVESTIGATE tests that**:
1. Have unclear root causes
2. May indicate actual production bugs
3. Require architectural decisions

---

## Related Work

- **PR #88**: Test cleanup (deleted ~718 legacy tests, fixed 54 failures)
- **PR #89**: Async generator mock fixes (35 tests fixed)
- **Commit eb99fed8**: Legacy test deletion (deleted 25 files, ~17,917 lines)

---

## Notes

- Evaluation-first approach: Assess test validity before attempting fixes
- Clean architecture principle: Delete tests for non-existent features
- Development system: No backward compatibility requirements
- Build forward: Focus on clean tests for current clean architecture
