# Module Organization Migration Status

**Date**: 2026-01-10  
**Status**: In Progress (90% Complete)

## Completed ✅

1. **Created contracts.py for Vertical Modules**:
   - ✅ Case module: `ICaseRepository`, `ICaseService`
   - ✅ Auth module: `IAuthService`, `IUserQuery`
   - ✅ Knowledge module: `IKnowledgeService`, `IKnowledgeQuery`

2. **Migrated Evidence Module**:
   - ✅ `EvidenceService` now uses `ICaseRepository` instead of `EvidenceRepository`
   - ✅ Updated DI container to pass `case_repository` to `EvidenceService`
   - ✅ Fixed all `EvidenceService` unit tests to use `case_repository`
   - ✅ Added `standalone_evidence` methods to `ICaseRepository` contract
   - ✅ Implemented `standalone_evidence` methods in `InMemoryCaseRepository` and `PostgreSQLHybridCaseRepository`

3. **Migrated Agent Module**:
   - ✅ `AgentOrchestrationService` now uses `ICaseRepository` instead of `AgentExecutionRepository`
   - ✅ Updated `ServiceFactory` to pass `case_repository` instead of `execution_repo`
   - ✅ Added `agent_executions` methods to `ICaseRepository` contract
   - ✅ Implemented `agent_executions` methods in `InMemoryCaseRepository` and `PostgreSQLHybridCaseRepository`

4. **Fixed Issues**:
   - ✅ Fixed UUID comparison bugs in `InMemoryCaseRepository.list_standalone_evidence`
   - ✅ Fixed test failures in `test_case_repository_agent_executions.py`
   - ✅ Fixed `EvidenceService` tests to use `case_repository` parameter

## Remaining Tasks ⏳

### 1. Remove Infrastructure Directories (Domain Services)

**Evidence Module** (`modules/evidence/infrastructure/`):
- ❌ **TODO**: Remove `persistence/evidence_repository.py` (migrated to Case)
- ⚠️ **PENDING**: Move `storage_adapter.py` out of `infrastructure/` (file storage adapter, not persistence)
  - **Option A**: Move to `domain/adapters/` (recommended)
  - **Option B**: Move to module root (not ideal)
  - **Option C**: Keep but document as exception (not aligned with design)

**Agent Module** (`modules/agent/infrastructure/`):
- ❌ **TODO**: Remove `persistence/agent_execution_repository.py` (migrated to Case)

**Blockers**:
- `EvidenceStorageAdapter` is still used in production (`container/providers/services.py`)
- Legacy services (`APICaseService`, `APIInvestigationSessionService`) still reference old repositories
- Many tests still import from module infrastructure directories

### 2. Update Legacy Services

**Legacy Services** (in `faultmaven/services/`):
- ❌ `APICaseService` still uses `EvidenceRepository` and `AgentExecutionRepository`
- ❌ `APIInvestigationSessionService` still uses `AgentExecutionRepository`
- ❌ `APIEvidenceArtifactService` still uses `EvidenceRepository`

**Note**: These are older service wrappers. The module services (`EvidenceService`, `AgentOrchestrationService`) have been migrated.

### 3. Update ServiceFactory

**ServiceFactory** (`faultmaven/services/service_factory.py`):
- ⚠️ Still creates `self.execution_repo` and `self.evidence_repo`
- ⚠️ Still passes them to legacy services
- ✅ `AgentOrchestrationService` creation updated to use `case_repository` only

## Design Compliance

### Current State vs. Target State

**Evidence Module** (Domain Service):
- **Target**: `domain/` + `api/` only, NO `infrastructure/`
- **Current**: Has `infrastructure/persistence/` (should be removed) + `infrastructure/storage_adapter.py` (needs relocation)

**Agent Module** (Domain Service):
- **Target**: `domain/` + `api/` only, NO `infrastructure/`
- **Current**: Has `infrastructure/persistence/` (should be removed)

**Case Module** (Vertical):
- **Target**: `contracts.py` + `domain/` + `api/` + `infrastructure/`
- **Current**: ✅ Has all required components, including `contracts.py`

## Recommendations

### Immediate Next Steps

1. **Remove Persistence Repositories**:
   - Delete `modules/evidence/infrastructure/persistence/evidence_repository.py`
   - Delete `modules/agent/infrastructure/persistence/agent_execution_repository.py`
   - Update `__init__.py` files to remove exports
   - Note: Tests can continue to import from old locations temporarily if needed

2. **Move EvidenceStorageAdapter**:
   - Create `modules/evidence/domain/adapters/` directory
   - Move `storage_adapter.py` to `domain/adapters/storage_adapter.py`
   - Update import in `container/providers/services.py`
   - Update `modules/evidence/infrastructure/__init__.py` to remove export (or remove directory if empty)

3. **Remove Empty Infrastructure Directories**:
   - After moving `EvidenceStorageAdapter`, remove `modules/evidence/infrastructure/` if empty
   - Remove `modules/agent/infrastructure/persistence/` directory
   - Remove `modules/agent/infrastructure/` if empty

### Future Work (Not Blocking)

1. **Migrate Legacy Services** (optional, low priority):
   - Update `APICaseService`, `APIInvestigationSessionService`, `APIEvidenceArtifactService` to use `ICaseRepository`
   - These services are older wrappers and may be deprecated in future refactoring

2. **Update Tests** (can be done incrementally):
   - Tests that directly test repository implementations can be kept
   - Tests that use module infrastructure can be updated to import from Case module or use mocks

## Summary

**Module Services Migration**: ✅ **COMPLETE**
- `EvidenceService` ✅ Migrated
- `AgentOrchestrationService` ✅ Migrated

**Infrastructure Cleanup**: ⏳ **IN PROGRESS** (90% complete)
- Persistence repositories: ✅ Functionally migrated, ❌ Files still exist
- Storage adapter: ⚠️ Needs relocation (not persistence, but still infrastructure)

**Design Compliance**: ⚠️ **NEARLY COMPLETE**
- Vertical modules: ✅ Compliant (Case, Auth, Knowledge)
- Domain services: ⚠️ Need infrastructure/ removal (Evidence, Agent, Report)

## Risk Assessment

**Low Risk**:
- Removing persistence repositories (migrated, not used by module services)
- Moving `EvidenceStorageAdapter` (simple file move + import update)

**Medium Risk**:
- Legacy services still use old repositories (but these are not part of module structure)
- Tests may need updates (but tests can be updated incrementally)

**Mitigation**:
- Keep old repository files temporarily if needed for legacy services
- Update imports incrementally
- Run tests after each change to verify no breakage
