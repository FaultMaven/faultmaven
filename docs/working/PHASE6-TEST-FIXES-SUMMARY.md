# Phase 6 Integration Test Fixes - Summary

**Branch:** `fix/integration-tests-phase6`
**Date:** 2026-01-10
**Status:** In Progress

## Overview

Phase 6 focused on fixing the highest-impact test failures, particularly the TestClient lifespan issues affecting 350+ tests.

## Commits

### 1. Fix import errors in report routes module
**Impact:** Fixed 18 test collection errors
**Files Modified:**
- `faultmaven/modules/report/api/routes.py` - Added missing imports
- `tests/utils.py` - Created shared utility functions

**Changes:**
- Added `IReportStore` and `get_report_store` imports to report routes
- Created `tests/utils.py` with ID generation functions (generate_case_id, generate_session_id, etc.)
- Resolved ModuleNotFoundError that prevented tests from collecting

### 2. Fix TestClient lifespan issues in case API integration tests
**Impact:** Fixed 8 tests (12 now passing, 24 still failing in test_cases_api.py)
**Files Modified:**
- `tests/integration/api/test_cases_api.py` - Complete async conversion

**Changes:**
- **Replaced sync TestClient with async AsyncClient + ASGITransport:**
  ```python
  # OLD (broken):
  client = TestClient(app)

  # NEW (working):
  async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
      yield client
  ```

- **Converted all 36 test methods to async:**
  - Changed `def test_*` to `async def test_*`
  - Added `await` before all `client.post/get/patch/delete` calls

- **Fixed dependency overrides:**
  - Override actual wrapper functions used by routes (`_di_get_case_service_dependency`)
  - Added proper cleanup to clear overrides after tests

**Key Learning:**
The TestClient lifespan issue was caused by:
1. FastAPI's async lifespan context managers not being properly initialized
2. Dependency overrides not working due to wrapper function re-imports
3. Sync TestClient not handling async app lifecycle

## Test Results

### Before Phase 6:
- **Total:** 404 failed, 281 passed, 129 errors
- **Collection errors:** 18 (blocking all API tests)

### After Phase 6 (Current):
- **test_cases_api.py:** 12 passing, 24 failing (was 32 failing)
- **Collection errors:** 0
- **Progress:** +8 tests fixed in one file

## Remaining Issues

### 1. TestClient Lifespan Issues (~340+ failures remaining)
**12 more API integration test files need the same fix:**
- test_admin_api.py
- test_admin_authorization.py
- test_agent_api.py
- test_auth_api.py (has async_client but wrong syntax)
- test_evidence_api.py
- test_jwt_protected_endpoints.py
- test_organization_authorization.py
- test_organizations_api.py
- test_reports_api.py
- test_session_enhancement_api.py
- test_sessions_api.py
- test_users_api.py

**Fix Pattern:**
1. Import `AsyncClient, ASGITransport` from httpx
2. Update client fixture to use `AsyncClient(transport=ASGITransport(app=app), ...)`
3. Convert all test methods to async
4. Add `await` before all client calls
5. Fix dependency overrides to use wrapper functions if needed

### 2. Test Data/Schema Mismatches (24 failures in test_cases_api.py)
**Examples:**
- Response has `cases` but test expects `items`
- HTTP 405 Method Not Allowed (wrong HTTP method)
- Missing required fields in request/response

**Priority:** Medium (fix after TestClient conversion)

### 3. Other Categories
- **DIContainer API changes** (~6 errors)
- **Pydantic validation** (~50+ failures)
- **SQLAlchemy concurrency** (1 error)

## Recommendations for Phase 7

**Option 1: Systematic TestClient Fixes (Highest Impact)**
- Apply AsyncClient pattern to all 12 remaining API test files
- Potential to fix 340+ tests
- Estimated effort: 2-3 hours (pattern is established)
- Use automation where possible (sed scripts for async conversion)

**Option 2: Quick Wins Approach**
- Fix test data/schema mismatches in test_cases_api.py (24 tests)
- Fix DIContainer API errors (~6 tests)
- Estimated effort: 1 hour
- Lower total impact but faster results

**Recommended Approach:**
1. **Phase 7a:** Apply AsyncClient fix to 5-6 more test files (highest-impact)
2. **Phase 7b:** Fix remaining test data issues
3. **Phase 8:** DIContainer and Pydantic validation fixes

## Technical Notes

### AsyncClient Syntax
```python
# Correct (httpx 0.24+):
from httpx import AsyncClient, ASGITransport

async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
    yield client

# WRONG (doesn't work):
async with AsyncClient(app=app, base_url="http://test") as client:
    yield client
```

### Dependency Override Pattern
```python
# If route uses wrapper function:
from faultmaven.modules.case.api.routes import _di_get_case_service_dependency

app.dependency_overrides[_di_get_case_service_dependency] = mock_service

# For direct dependencies:
from faultmaven.api.v1.auth_dependencies import require_authentication

app.dependency_overrides[require_authentication] = mock_user
```

## Files Modified in Phase 6

1. `faultmaven/modules/report/api/routes.py`
2. `tests/utils.py` (new file)
3. `tests/integration/api/test_cases_api.py`

## Automation Scripts for Phase 7

### Convert test file to async (template):
```bash
# 1. Add imports
sed -i 's/from fastapi.testclient import TestClient/from httpx import AsyncClient, ASGITransport/' test_file.py

# 2. Convert test methods to async
sed -i 's/^    def test_/    async def test_/g' test_file.py

# 3. Add await before client calls
sed -i 's/response = client\.\(post\|get\|patch\|delete\|put\)/response = await client.\1/g' test_file.py

# 4. Manual: Update client fixture to use ASGITransport
# 5. Manual: Fix dependency overrides if needed
```

## Next Steps

1. Choose approach for Phase 7 (systematic vs quick wins)
2. If systematic: Create batch script to convert all 12 test files
3. If quick wins: Focus on test_cases_api.py data issues
4. Run full test suite after changes
5. Document results and iterate

## Success Metrics

- **Phase 6:** Fixed 26 tests (18 collection + 8 functional)
- **Phase 7 Target:** Fix 100-150 more tests
- **Overall Goal:** Bring failures below 300 (from current 404)
