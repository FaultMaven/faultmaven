# Import Fix Mapping for Test Collection Errors

**Date**: 2026-01-01
**Issue**: 44 test collection errors due to model refactoring in Phase 1 PRs
**Root Cause**: Tests reference old import paths that were changed during domain model reorganization

---

## Import Fixes Required

### 1. CaseCreateRequest
- **Old Import**: `from faultmaven.models.case import CaseCreateRequest`
- **New Import**: `from faultmaven.models.api_models import CaseCreateRequest`
- **Location**: [api_models.py:23](../faultmaven/models/api_models.py#L23)
- **Affected Files**:
  - `tests/api/test_case_endpoints.py`
  - `tests/api/test_contract_compliance_focused.py`
  - Any other test importing CaseCreateRequest from case module

### 2. CaseMessage
- **Old Import**: `from faultmaven.models.case import CaseMessage`
- **New Import**: `from faultmaven.models.api_models import CaseMessage`
- **Location**: [api_models.py:382](../faultmaven/models/api_models.py#L382)
- **Affected Files**:
  - `tests/services/test_case_service.py`
  - Any other test importing CaseMessage from case module

### 3. CasePriority → CaseSeverity (RENAMED)
- **Old Import**: `from faultmaven.models.case import CasePriority`
- **New Import**: `from faultmaven.models.case import CaseSeverity`
- **Change**: Enum was renamed from `CasePriority` to `CaseSeverity`
- **Location**: [case.py](../faultmaven/models/case.py) (search for "class CaseSeverity")
- **Affected Files**:
  - `tests/api/test_contract_compliance_focused.py`
  - Any test using `CasePriority.MEDIUM` → change to `CaseSeverity.MEDIUM`
  - Any test using `CasePriority.HIGH` → change to `CaseSeverity.HIGH`

### 4. CaseDiagnosticState (REMOVED)
- **Old Import**: `from faultmaven.models.case import CaseDiagnosticState`
- **Status**: Class no longer exists in refactored codebase
- **Action Required**:
  - Investigate what replaced this class
  - Check if tests should use `InvestigationState` instead
  - May need to refactor tests to use new domain models
- **Affected Files**:
  - `tests/unit/services/evidence/test_lifecycle.py`

### 5. _calculate_evidence_completeness → _calculate_overall_evidence_completeness (RENAMED)
- **Old Import**: `from faultmaven.core.investigation.working_conclusion_generator import _calculate_evidence_completeness`
- **New Import**: `from faultmaven.core.investigation.working_conclusion_generator import _calculate_overall_evidence_completeness`
- **Change**: Function was renamed
- **Location**: [working_conclusion_generator.py](../faultmaven/core/investigation/working_conclusion_generator.py)
- **Affected Files**:
  - `tests/unit/investigation/test_working_conclusion_generator.py`
  - Update all calls from `_calculate_evidence_completeness(...)` to `_calculate_overall_evidence_completeness(...)`

### 6. get_structured_output_schema_prompt (REMOVED)
- **Old Import**: `from faultmaven.prompts.phase3_structured_output import get_structured_output_schema_prompt`
- **Status**: Function/module no longer exists
- **Action Required**:
  - File `phase3_structured_output.py` does not exist
  - Investigate what replaced this functionality
  - May need to skip/remove obsolete tests
- **Affected Files**:
  - `tests/prompts/test_v3_prompts.py`

---

## Import Fix Strategy

### Phase 1: Simple Renames (Quick Fixes)
1. ✅ `CaseCreateRequest`: Change import from `case` to `api_models`
2. ✅ `CaseMessage`: Change import from `case` to `api_models`
3. ✅ `CasePriority` → `CaseSeverity`: Rename enum throughout tests
4. ✅ `_calculate_evidence_completeness` → `_calculate_overall_evidence_completeness`: Rename function

### Phase 2: Refactored/Removed Models (Investigation Required)
5. ⚠️ `CaseDiagnosticState`: Determine replacement model
6. ⚠️ `get_structured_output_schema_prompt`: Determine if tests should be removed or updated

---

## models/__init__.py Analysis

The [models/__init__.py](../faultmaven/models/__init__.py) file attempts to import these from `case.py`:

```python
# Lines 67-81
from .case import (
    Case,
    CaseMessage,        # ❌ Does NOT exist in case.py (exists in api_models.py)
    CaseParticipant,
    CaseContext,
    CaseDiagnosticState,  # ❌ Does NOT exist in case.py
    CaseStatus,
    CasePriority,        # ❌ Does NOT exist in case.py (renamed to CaseSeverity)
    MessageType,
    CaseCreateRequest,   # ❌ Does NOT exist in case.py (exists in api_models.py)
    CaseUpdateRequest,   # ❌ Likely does NOT exist in case.py (check api_models.py)
    CaseListFilter,
    CaseSearchRequest,
    CaseSummary          # ❌ Likely does NOT exist in case.py (check api_models.py)
)
```

**Root Cause**: The `models/__init__.py` has stale imports wrapped in try/except, so import errors are silently swallowed and `CASE_MODELS_AVAILABLE = False`.

**Fix Required**: Update `models/__init__.py` to import from correct locations:
- Import API models from `api_models.py`, not `case.py`
- Change `CasePriority` to `CaseSeverity`
- Remove `CaseDiagnosticState` if obsolete

---

## Next Steps

1. Update `models/__init__.py` to fix root cause
2. Apply Phase 1 simple renames in all test files
3. Investigate Phase 2 refactored models
4. Run test collection again to verify all 44 errors resolved
5. Run full test suite to ensure no regressions

---

**Document Status**: Draft mapping, needs verification before applying fixes
