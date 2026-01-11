# Cleanup Plan: Remove Old Repositories

**Date**: 2026-01-10  
**Status**: Planning  
**Goal**: Complete migration to ICaseRepository and remove old repository files

## Current Situation

### Active API Services (NOT Legacy - Currently Used)

1. **APICaseService** (`faultmaven/services/case_service.py`)
   - **Used by**: `faultmaven/api/routes/cases.py` (9 endpoints)
   - **Current dependencies**: `evidence_repo`, `execution_repo`
   - **Needs migration**: Replace `evidence_repo` and `execution_repo` with `case_repo` using ICaseRepository methods

2. **APIInvestigationSessionService** (`faultmaven/services/investigation_session_service.py`)
   - **Used by**: `faultmaven/api/routes/sessions.py` (8 endpoints), `AgentOrchestrationService`
   - **Current dependencies**: `execution_repo`
   - **Needs migration**: Replace `execution_repo` with `case_repo` using ICaseRepository methods

3. **APIEvidenceArtifactService** (`faultmaven/services/evidence_artifact_service.py`)
   - **Used by**: `faultmaven/api/routes/evidence.py` (8 endpoints)
   - **Current dependencies**: `evidence_repo`
   - **Needs migration**: Replace `evidence_repo` with `case_repo` using ICaseRepository methods

### Module Services (Already Migrated ✅)

- **EvidenceService** (`faultmaven/modules/evidence/domain/services/evidence_service.py`) - ✅ **DONE**
- **AgentOrchestrationService** (`faultmaven/modules/agent/domain/services/agent_orchestration_service.py`) - ✅ **DONE**

## Migration Tasks

### 1. Migrate APICaseService
**File**: `faultmaven/services/case_service.py`

**Changes**:
- Remove `evidence_repo: EvidenceArtifactRepository` parameter
- Remove `execution_repo: AgentExecutionRepository` parameter
- Add `case_repo: ICaseRepository` parameter (or use existing `case_repo` if already present)
- Replace `self.evidence_repo.list_evidence_by_case(case_id)` → `self.case_repo.list_standalone_evidence(EvidenceListFilter(case_id=case_id))`
- Replace `self.execution_repo.list_executions_by_case(case_id)` → `self.case_repo.list_agent_executions_by_case(case_id)`

**Lines to change**:
- Line 555: `evidence_list, _ = await self.evidence_repo.list_evidence_by_case(case_id)`
- Line 563: `executions, _ = await self.execution_repo.list_executions_by_case(case_id)`

### 2. Migrate APIInvestigationSessionService
**File**: `faultmaven/services/investigation_session_service.py`

**Changes**:
- Remove `execution_repo: AgentExecutionRepository` parameter
- Add `case_repo: ICaseRepository` parameter (check if already present)
- Replace `self.execution_repo.list_executions_by_case(session.case_id)` → `self.case_repo.list_agent_executions_by_case(session.case_id)`
- Replace `self.execution_repo.get_tool_calls_for_execution(execution_id)` → `self.case_repo.get_agent_tool_calls_for_execution(execution_id)`
- Replace `self.execution_repo.get_execution(execution_id)` → `self.case_repo.get_agent_execution(execution_id)`

**Lines to change**:
- Line 803: `executions, _ = await self.execution_repo.list_executions_by_case(session.case_id)`
- Line 816: `await self.execution_repo.get_tool_calls_for_execution(execution.execution_id)`
- Line 896: `execution = await self.execution_repo.get_execution(execution_id)`

### 3. Migrate APIEvidenceArtifactService
**File**: `faultmaven/services/evidence_artifact_service.py`

**Changes**:
- Remove `evidence_repo: EvidenceArtifactRepository` parameter
- Add `case_repo: ICaseRepository` parameter
- Replace all `self.evidence_repo.*` calls with `self.case_repo.*` equivalents:
  - `get_evidence` → `get_standalone_evidence`
  - `create_evidence` → `create_standalone_evidence`
  - `update_evidence` → (check if needed - might need update method in ICaseRepository)
  - `delete_evidence` → `delete_standalone_evidence`
  - `list_evidence_by_case` → `list_standalone_evidence`
  - `set_primary_evidence` → (check if this is in ICaseRepository or can be removed)
  - `get_primary_evidence` → (check if this is in ICaseRepository or can be removed)

**Note**: Some methods like `set_primary_evidence` and `get_primary_evidence` might not exist in ICaseRepository. Need to check if these are needed or can be removed.

### 4. Update ServiceFactory
**File**: `faultmaven/services/service_factory.py`

**Changes**:
- Remove `self.evidence_repo` creation
- Remove `self.execution_repo` creation
- Update `create_case_service()` to not pass `evidence_repo` and `execution_repo`
- Update `create_investigation_session_service()` to not pass `execution_repo`
- Update `create_evidence_artifact_service()` to not pass `evidence_repo`, pass `case_repo` instead

### 5. Update Dependencies
**File**: `faultmaven/api/dependencies.py`

**Changes**:
- Update `get_api_case_service()` to not pass `evidence_repo` and `execution_repo`
- Update `get_investigation_session_service()` to not pass `execution_repo`
- Update `get_evidence_artifact_service()` to not pass `evidence_repo`, pass `case_repo` instead

### 6. Update Bootstrap
**File**: `faultmaven/bootstrap/service_factories.py`

**Changes**:
- Update service creation to use `case_repo` instead of old repositories

### 7. Remove Old Repository Files

**Files to delete**:
- `faultmaven/modules/evidence/infrastructure/persistence/evidence_repository.py`
- `faultmaven/modules/evidence/infrastructure/persistence/__init__.py`
- `faultmaven/modules/agent/infrastructure/persistence/agent_execution_repository.py`
- `faultmaven/modules/agent/infrastructure/persistence/__init__.py` (if empty)
- `faultmaven/modules/evidence/infrastructure/__init__.py` (after cleaning)
- `faultmaven/modules/agent/infrastructure/__init__.py` (after cleaning)
- Remove `infrastructure/` directories if empty

**Also check**:
- `faultmaven/infrastructure/persistence/evidence_artifact_repository.py` - might be a different file, check if used
- Any imports in `faultmaven/infrastructure/persistence/repository_factory.py`

## Order of Operations

1. **Migrate API Services** (APICaseService, APIInvestigationSessionService, APIEvidenceArtifactService)
2. **Update ServiceFactory and Dependencies**
3. **Update Tests** for migrated services
4. **Verify Everything Works**
5. **Remove Old Repository Files**
6. **Clean Up Empty Directories**

## Risks

- **Breaking Changes**: API services are actively used, so migration must be careful
- **Test Updates**: Many tests will need updating
- **Method Mapping**: Some methods might not have direct equivalents (e.g., `set_primary_evidence`)

## Benefits

- ✅ Clean architecture aligned with module organization design
- ✅ Single source of truth for data access (ICaseRepository)
- ✅ Removes technical debt
- ✅ Easier to maintain and test
- ✅ Clear separation of concerns
