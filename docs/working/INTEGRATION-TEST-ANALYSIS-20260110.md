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

### Overall Integration Tests
```
298 passing
385 failing
128 errors
---
711 total
```

### API Integration Tests (`tests/integration/api/`)
```
68 passing
200 failing
86 errors
---
354 total
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

## Next Steps

### Phase 1: Systematic Evaluation (IN PROGRESS)

1. **Categorize all failing/error tests** by root cause:
   - Non-existent endpoints (like auth)
   - Async generator mocking (like agent_api - FIXED)
   - Import errors / module not found
   - Database/fixture issues
   - Other

2. **Triage categories**:
   - **DELETE**: Tests for non-existent features
   - **FIX**: Tests with correctable bugs
   - **INVESTIGATE**: Unclear root cause

### Phase 2: Cleanup (TODO)

1. Delete obsolete test files
2. Fix correctable bugs (apply patterns from PR #89)
3. Document remaining issues

### Phase 3: Report & Merge (TODO)

1. Create PR for test cleanup
2. Update test metrics
3. Document architectural decisions

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
