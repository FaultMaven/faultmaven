# Migration Progress: API Services to ICaseRepository

**Date**: 2026-01-10  
**Status**: ~90% Complete - API services migrated, remaining references to fix

## Completed ✅

1. **APICaseService** - ✅ Fully migrated
   - Removed `evidence_repo` and `execution_repo` parameters
   - Updated to use `case_repo.list_standalone_evidence()` and `case_repo.list_agent_executions_by_case()`
   - Compiles successfully

2. **APIInvestigationSessionService** - ✅ Fully migrated
   - Removed `execution_repo` parameter
   - Updated to use `case_repo.list_agent_executions_by_case()`, `case_repo.get_agent_tool_calls_for_execution()`, `case_repo.get_agent_execution()`
   - Compiles successfully

3. **ICaseRepository Contract** - ✅ Methods added
   - Added `update_standalone_evidence()`
   - Added `set_primary_evidence()`
   - Added `get_primary_evidence()`

4. **InMemoryCaseRepository** - ✅ Implementations added
   - Implemented `update_standalone_evidence()`
   - Implemented `set_primary_evidence()`
   - Implemented `get_primary_evidence()`

5. **PostgreSQLHybridCaseRepository** - ✅ Stubs added
   - Added stub methods with `NotImplementedError`

## In Progress 🔄

1. **APIEvidenceArtifactService** - ~90% migrated
   - ✅ Constructor updated (removed `evidence_repo`, kept `case_repo`)
   - ✅ Most methods updated
   - ⚠️ 5 remaining references to `self.evidence_repo` need to be updated:
     - Line 440: `set_primary_evidence` in `update_evidence()`
     - Line 463: `update_evidence()` 
     - Line 466: `get_evidence()` 
     - Line 769: `list_evidence_by_case()` in `get_evidence_statistics()`
     - Line 861: `list_evidence_by_case()` in `delete_all_evidence_for_case()`

## Next Steps

1. Fix remaining 5 references in `APIEvidenceArtifactService`
2. Update `ServiceFactory` to remove old repository parameters
3. Update API dependencies (`faultmaven/api/dependencies.py`)
4. Update bootstrap service factories
5. Remove old repository files
6. Run tests to verify everything works
