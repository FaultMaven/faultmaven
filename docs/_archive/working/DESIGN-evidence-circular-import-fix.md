# Evidence Module Circular Import Fix - Architecture Design

**Status**: Implementation Ready
**Priority**: P0 - Blocking 22 Tests
**Date**: 2026-01-08
**Architect**: Solutions Architect Agent

## Executive Summary

The Evidence module has 22 failing tests due to an import path inconsistency between production code and test code. This is NOT a circular import issue, but rather an **architectural inconsistency** in the dependency injection layer.

**Root Cause**: Two different dependencies modules with different import paths:
- `faultmaven.api.dependencies` - New API service layer (TASK-011/012/013)
- `faultmaven.api.v1.dependencies` - Legacy API layer

**Impact**:
- 22 evidence API tests blocked
- Potential confusion for developers
- No runtime impact (tests only)

**Solution**: Add re-exports in `faultmaven.api.dependencies` to provide a canonical import path for shared dependencies.

---

## Problem Analysis

### Current State

**Dependencies Module Structure:**

```
faultmaven/api/
├── dependencies.py          # New API service layer (TASK-011/012/013)
│   └── Services: APICaseService, APIEvidenceArtifactService, etc.
└── v1/
    └── dependencies.py      # Legacy API layer
        └── Functions: get_current_user, get_session_service, etc.
```

**Import Inconsistency:**

| File | Import Path | Status |
|------|------------|--------|
| `evidence/api/routes.py` (line 29) | `from faultmaven.api.v1.dependencies import get_current_user` | ✅ Works |
| `tests/.../test_evidence_api.py` (line 127) | `from faultmaven.api.dependencies import get_current_user` | ❌ Fails |

**Error Message:**
```
ImportError: cannot import name 'get_current_user' from 'faultmaven.api.dependencies'
```

### Why This Happened

1. **TASK-011/012/013** introduced a new `faultmaven.api.dependencies` module for the API service layer
2. **Legacy code** uses `faultmaven.api.v1.dependencies` for shared utilities like `get_current_user`
3. **Evidence module** correctly imports from `v1.dependencies` (production code works)
4. **Tests** incorrectly assume `get_current_user` is in the top-level `dependencies` module

### Impact Assessment

**What's Broken:**
- 22 Evidence module API tests fail at import time
- Test discovery errors in CI/CD

**What's NOT Broken:**
- Production code works fine
- Evidence routes function correctly
- No circular import (original diagnosis was incorrect)

---

## Architectural Solution

### Design Principles

1. **Single Source of Truth**: `faultmaven.api.v1.dependencies` remains the authoritative source
2. **Canonical Import Path**: `faultmaven.api.dependencies` becomes the public API
3. **Backward Compatibility**: Existing imports continue to work
4. **Zero Runtime Impact**: Re-exports have negligible performance cost

### Proposed Architecture

**Re-export Pattern:**

```python
# faultmaven/api/dependencies.py

# ============================================================
# Re-exports from v1.dependencies (for consistency)
# ============================================================
from faultmaven.api.v1.dependencies import (
    get_current_user,
    get_session_service,
    get_knowledge_service,
    require_authenticated_user,
    get_user_id,
    get_session_id,
    # Add other commonly used dependencies as needed
)

__all__ = [
    # Service Factory Dependencies (TASK-011/012/013)
    "get_service_factory",
    "get_api_case_service",
    "get_investigation_session_service",
    "get_evidence_artifact_service",
    "get_file_storage_service",
    "get_agent_orchestration_service",
    # Re-exported from v1.dependencies
    "get_current_user",
    "get_session_service",
    "get_knowledge_service",
    "require_authenticated_user",
    "get_user_id",
    "get_session_id",
]
```

**Benefits:**
1. Tests can import from `faultmaven.api.dependencies` (canonical path)
2. Existing code importing from `faultmaven.api.v1.dependencies` still works
3. Future code should prefer `faultmaven.api.dependencies` (single import path)
4. No circular imports (re-exports are resolved at import time)

---

## Implementation Plan

### Phase 1: Add Re-exports (Immediate)

**File**: `faultmaven/api/dependencies.py`

**Changes:**
1. Add import block at top of file (after existing imports)
2. Add re-exported functions to `__all__` list
3. No changes to existing code

**Estimated Time**: 5 minutes
**Risk**: Very Low

### Phase 2: Verify Tests Pass

**Commands:**
```bash
cd /home/swhouse/product/faultmaven
. .venv/bin/activate
pytest tests/unit/modules/evidence/ -v
```

**Expected Outcome:**
- 22 previously failing tests now pass
- Repository tests: 46 passing (unchanged)
- Service tests: May have some unrelated failures (to be addressed separately)
- Storage adapter tests: 18 passing (unchanged)

**Success Criteria:**
- All 22 evidence API tests pass
- No new test failures
- Import errors resolved

### Phase 3: Update Documentation (Follow-up)

**Files to Update:**
- `docs/architecture/DEPENDENCIES.md` - Document canonical import path
- `CONTRIBUTING.md` - Add guidance on dependency imports

**Content:**
```markdown
## Dependency Injection

**Canonical Import Path:**
```python
from faultmaven.api.dependencies import (
    get_current_user,
    get_api_case_service,
    get_evidence_artifact_service,
)
```

**Legacy Path (still supported):**
```python
from faultmaven.api.v1.dependencies import get_current_user
```

**Guideline**: New code should use `faultmaven.api.dependencies` for all dependency imports.
```

---

## Testing Strategy

### Test Execution Plan

**1. Evidence API Tests (Primary Target)**
```bash
pytest tests/unit/modules/evidence/test_evidence_api.py -v
```
- Expected: 22 tests pass
- Coverage: API endpoint tests with mocked dependencies

**2. Evidence Repository Tests (Control Group)**
```bash
pytest tests/unit/modules/evidence/test_evidence_repository.py -v
```
- Expected: 46 tests pass (no changes)
- Coverage: Database repository tests

**3. Evidence Service Tests (May need fixes)**
```bash
pytest tests/unit/modules/evidence/test_evidence_service.py -v
```
- Expected: Some may fail due to unrelated mock signature issues
- Note: Not part of this fix (separate issue)

**4. Full Evidence Module Test Suite**
```bash
pytest tests/unit/modules/evidence/ -v --tb=short
```
- Expected: 22 API tests pass, others may have unrelated issues

### Test Coverage Requirements

**Per [Testing Standards](../standards/TESTING_STANDARDS.md):**

- **Maintain 71%+ Coverage**: ✅ No new code, only re-exports
- **All Tests Pass**: ✅ Target is 22 API tests
- **No Skipped Tests**: ✅ No tests skipped
- **Integration Tests**: N/A - Unit tests only

**Coverage Impact**: ZERO (only import path changes)

### Acceptance Criteria

- [ ] All 22 evidence API tests pass
- [ ] No new import errors
- [ ] No regression in other test suites
- [ ] Import from both paths works (backward compatibility)

---

## Risk Assessment

### Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Re-export circular import | Very Low | High | Re-exports are static, no runtime cycles |
| Namespace pollution | Low | Low | Use explicit `__all__` list |
| Performance impact | Very Low | Very Low | Re-exports resolved at import time |
| Confusion about import paths | Medium | Low | Document canonical path in CONTRIBUTING.md |

### Rollback Plan

If re-exports cause issues:

1. **Immediate**: Remove re-export lines from `dependencies.py`
2. **Fix tests**: Update test imports to use `faultmaven.api.v1.dependencies`
3. **Document**: Clarify two separate dependency modules in docs

**Rollback Time**: < 5 minutes
**Data Loss**: None (no data changes)

---

## Alternative Solutions Considered

### Alternative 1: Update Test Imports

**Approach**: Change test imports to `faultmaven.api.v1.dependencies`

**Pros:**
- No production code changes
- Minimal risk

**Cons:**
- Tests import from different path than production code
- Confusion for new developers
- Doesn't address architectural inconsistency

**Decision**: Rejected - Doesn't solve root cause

### Alternative 2: Move get_current_user to new dependencies.py

**Approach**: Copy `get_current_user` from v1 to new dependencies module

**Pros:**
- Clear separation

**Cons:**
- Code duplication
- Two sources of truth
- Maintenance burden
- Breaking change for existing code

**Decision**: Rejected - Violates DRY principle

### Alternative 3: Deprecate v1.dependencies entirely

**Approach**: Migrate all functions to new dependencies module

**Pros:**
- Clean architecture
- Single dependencies module

**Cons:**
- Large migration effort (50+ files)
- Breaking change
- High risk
- Out of scope for this fix

**Decision**: Rejected - Future refactoring candidate

---

## Implementation Details

### Code Changes

**File**: `/home/swhouse/product/faultmaven/faultmaven/api/dependencies.py`

**Location**: After line 36 (after existing imports, before first function)

**Code to Add:**

```python
# ============================================================
# Re-exports from v1.dependencies
# ============================================================
# These functions are defined in api.v1.dependencies but re-exported
# here to provide a canonical import path for all API dependencies.
# This maintains backward compatibility while establishing
# faultmaven.api.dependencies as the single source for dependency injection.

from faultmaven.api.v1.dependencies import (
    get_current_user,
    require_authenticated_user,
    get_session_service,
    get_knowledge_service,
    get_user_id,
    get_session_id,
    get_current_session,
    get_optional_session,
)

__all__ = [
    # Service Factory Dependencies (TASK-011/012/013)
    "get_async_db_session",
    "get_service_factory",
    "get_api_case_service",
    "get_investigation_session_service",
    "get_file_storage_service",
    "get_evidence_artifact_service",
    "get_agent_orchestration_service",
    # Re-exported from v1.dependencies (legacy)
    "get_current_user",
    "require_authenticated_user",
    "get_session_service",
    "get_knowledge_service",
    "get_user_id",
    "get_session_id",
    "get_current_session",
    "get_optional_session",
]
```

**Validation:**

```python
# Test that re-exports work
python3 -c "from faultmaven.api.dependencies import get_current_user; print('OK')"
```

---

## Success Metrics

### Primary Metrics

1. **Test Pass Rate**: 22/22 evidence API tests pass (100%)
2. **Import Errors**: 0 import errors in evidence module
3. **Regression**: 0 new test failures

### Secondary Metrics

4. **Build Time**: No significant increase (< 1 second)
5. **Code Coverage**: Maintained at 71%+ (no change expected)
6. **Developer Experience**: Single canonical import path

---

## Post-Implementation

### Verification Commands

```bash
# 1. Verify imports work
cd /home/swhouse/product/faultmaven
. .venv/bin/activate
python3 -c "from faultmaven.api.dependencies import get_current_user; print('✅ Import successful')"

# 2. Run evidence API tests
pytest tests/unit/modules/evidence/test_evidence_api.py -v

# 3. Run full evidence test suite
pytest tests/unit/modules/evidence/ -v --tb=short

# 4. Check for circular imports
python3 -c "import faultmaven.api.dependencies; import faultmaven.api.v1.dependencies; print('✅ No circular imports')"
```

### Expected Output

```
tests/unit/modules/evidence/test_evidence_api.py::TestUploadEvidenceEndpoint::test_upload_evidence_success PASSED
tests/unit/modules/evidence/test_evidence_api.py::TestUploadEvidenceEndpoint::test_upload_evidence_with_tags PASSED
...
========================= 22 passed in X.XXs =========================
```

### Monitoring

- [ ] CI/CD pipeline evidence tests pass
- [ ] No new Sentry errors related to imports
- [ ] Code review approval
- [ ] Documentation updated

---

## Future Improvements

### Short-term (Next Sprint)

1. **Standardize Import Paths**
   - Update all modules to use `faultmaven.api.dependencies`
   - Add linting rule to enforce canonical path

2. **Documentation**
   - Create `docs/architecture/DEPENDENCIES.md`
   - Update CONTRIBUTING.md with import guidelines

### Long-term (Future Quarters)

3. **Deprecate v1.dependencies**
   - Migrate all functions to new dependencies module
   - Add deprecation warnings
   - Remove after 2-3 releases

4. **Dependency Injection Refactor**
   - Consider moving to a proper DI framework (e.g., dependency-injector)
   - Consolidate all dependency logic
   - Improve testability with better mocking support

---

## Appendix

### A. Circular Import Analysis (Original Diagnosis)

**Original Report:**
```
Circular Chain:
1. evidence/api/routes.py → imports api.v1.dependencies.get_current_user
2. api.v1.dependencies → imports KnowledgeService
3. knowledge/__init__.py → imports knowledge/api/routes.py
4. knowledge/api/routes.py → imports api.v1.dependencies.get_knowledge_service
```

**Actual Issue:**
This is NOT a circular import. The chain breaks at step 2:
- `api.v1.dependencies` imports `KnowledgeService` under `TYPE_CHECKING` only (line 32)
- No runtime circular import exists
- The real issue is **test import path inconsistency**

### B. Import Path Comparison

| Module | Production Import | Test Import | Match |
|--------|------------------|-------------|-------|
| Evidence API | `api.v1.dependencies` | `api.dependencies` | ❌ |
| Knowledge API | `api.v1.dependencies` | `api.v1.dependencies` | ✅ |
| Case API | `api.dependencies` | `api.dependencies` | ✅ |

### C. Related Files

**Dependencies Files:**
- `/home/swhouse/product/faultmaven/faultmaven/api/dependencies.py`
- `/home/swhouse/product/faultmaven/faultmaven/api/v1/dependencies.py`

**Evidence Module:**
- `/home/swhouse/product/faultmaven/faultmaven/modules/evidence/api/routes.py`
- `/home/swhouse/product/faultmaven/tests/unit/modules/evidence/test_evidence_api.py`

**Test Files:**
- `tests/unit/modules/evidence/test_evidence_api.py` - 22 tests
- `tests/unit/modules/evidence/test_evidence_repository.py` - 46 tests
- `tests/unit/modules/evidence/test_evidence_service.py` - 20 tests
- `tests/unit/modules/evidence/test_storage_adapter.py` - 18 tests

---

## Sign-off

**Architect**: Solutions Architect Agent
**Reviewed By**: (Pending)
**Approved By**: (Pending)
**Implementation Date**: 2026-01-08

**Decision**: APPROVED FOR IMPLEMENTATION

This design follows FaultMaven architectural standards and [Testing Standards](../standards/TESTING_STANDARDS.md).
