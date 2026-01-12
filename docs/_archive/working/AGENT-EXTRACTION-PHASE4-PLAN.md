# Agent Module Extraction - Phase 4 Execution Plan

**Date**: 2026-01-07
**Status**: Phase 4 in progress - Import updates needed
**Completed**: Phases 1-3 (all files moved with backward-compat re-exports)

---

## Current State

### ✅ Completed (Phases 1-3)
- All 24 production files moved to `modules/agent/` using `git mv`
- Backward-compatible re-exports added for:
  - Models (`models/__init__.py`)
  - Events (`domain/__init__.py`)
  - Services (`services/__init__.py`, `services/domain/__init__.py`)
- All commits pushed to remote

### 🔲 Remaining Work (Phase 4)

## Files Needing Import Updates

Based on grep analysis:
- **7 files** import AgentOrchestrationService from old location
- **5 files** import investigation services from old location
- **45 files** import from tools (old location)
- **1 file** imports from agent API routes

**Total**: ~58 files need import updates

---

## Phase 4 Strategy

Given the backward-compatible re-exports are already in place, the codebase should work AS-IS without breaking. This allows us to take a measured approach:

### Option A: Incremental Cleanup (Recommended)
1. Test that backward-compat re-exports work
2. Update imports in batches
3. Run tests after each batch
4. Remove old files only after all imports updated
5. Remove backward-compat re-exports last

### Option B: Big Bang (Riskier)
1. Update all imports at once
2. Remove old files
3. Run tests and fix all failures
4. Remove backward-compat re-exports

---

## Recommended Next Steps

### Step 1: Verify Backward Compatibility Works (15 min)

```bash
# Test that services can be imported from both old and new locations
python3 -c "
from faultmaven.services import AgentOrchestrationService
from faultmaven.modules.agent.domain.services.agent_orchestration_service import AgentOrchestrationService as AOS
print('✓ Backward compatibility works')
"

# Run a quick test
pytest tests/unit/services/test_agent_orchestration_service.py -v
```

### Step 2: Update Critical Files First (1-2 hours)

**Priority 1: Service files themselves**
- Update imports in moved service files (they import models/events from old locations)
- Files: `agent_orchestration_service.py`, `investigation_orchestrator.py`, `investigation_service.py`

**Priority 2: DI Container**
- Update container registration to use new paths
- File: `container/providers/services.py`

**Priority 3: API Routes**
- Update API route file to import from new location
- File: Main FastAPI app (wherever agent routes are registered)

### Step 3: Decide on Tools Import Strategy (Decision Required)

**Two options for the 45 files importing from tools:**

**Option A: Leave backward-compat re-exports indefinitely**
- Pros: No breaking changes, gradual migration
- Cons: Technical debt

**Option B: Create tools package re-exports and update gradually**
- Pros: Clean migration path
- Cons: More work

**Option C: Update all 45 files in one go**
- Pros: Clean break
- Cons: High risk of breaking things

### Step 4: Test-Driven Approach (Recommended)

1. Run full test suite to establish baseline
2. Identify which tests are currently passing
3. Update imports in batches of 5-10 files
4. Re-run tests after each batch
5. Fix failures immediately

---

## Decision Points

### Decision 1: How to handle tools imports?

**Recommendation**: Keep backward-compat re-exports for tools and defer updating 45 files to a future PR. This allows us to:
- Complete the extraction without breaking changes
- Update tool imports incrementally over time
- Focus on critical path (services, container, API)

### Decision 2: When to remove old files?

**Recommendation**: Do NOT remove old files in this PR. Instead:
- Keep backward-compat re-exports
- Mark old files with deprecation warnings
- Remove in a follow-up PR after confirming everything works

### Decision 3: Update MODULE-EXTRACTION-STATUS now or later?

**Recommendation**: Update status to "86% complete" NOW, even if we keep backward-compat re-exports. The extraction is functionally complete - cleanup can be done later.

---

## Minimal Viable Phase 4 (Recommended Scope)

To complete the extraction without over-engineering:

### ✅ Do This (Essential)
1. **Update imports in moved service files** (3 files)
   - So they use new model/event paths internally
2. **Create Agent module README** (documentation)
3. **Update MODULE-EXTRACTION-STATUS.md** (71% → 86%)
4. **Commit and push** with clear message

### ⏸️ Defer This (Can be done later)
1. Updating 45 tool imports (keep backward-compat)
2. Removing old files (keep them, add deprecation warnings)
3. Removing backward-compat re-exports (leave in place)
4. Updating test imports (tests use backward-compat re-exports)

---

## Pragmatic Completion Criteria

The Agent module extraction is **COMPLETE** when:

- ✅ All files moved to vertical slice structure
- ✅ Backward-compatible re-exports in place
- ✅ Module structure documented
- ✅ Platform status updated (86% complete)
- ⏸️ Import cleanup (deferred to follow-up PR)

---

## Estimated Time

**Minimal Viable Phase 4**: 2-3 hours
**Full Phase 4 (with import updates)**: 1-2 days

**Recommendation**: Ship minimal viable version, create follow-up issue for import cleanup.

---

## Next Command

```bash
# Start with service file import updates
grep -n "from faultmaven.models\|from faultmaven.domain.events" \
  faultmaven/modules/agent/domain/services/*.py
```
