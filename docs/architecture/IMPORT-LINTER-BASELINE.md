# Import Linter Baseline - Phase 3 Week 13

**Date**: 2026-01-01
**Purpose**: Establish architectural violation baseline before Phase 3 refactoring
**Tool**: import-linter 2.9
**Configuration**: `.importlinter`

## Executive Summary

Import-linter has been configured to enforce critical architectural boundaries in the FaultMaven codebase. This baseline establishes the current state of violations before Phase 3 refactoring begins.

**Current Status:**
- ✅ **2 contracts KEPT** (zero violations)
- ❌ **1 contract BROKEN** (6 violations)
- 📊 **262 files analyzed, 614 dependencies**

## Contract Results

### Contract 1: Service Layer Independence ❌ BROKEN

**Status**: 6 violations
**Severity**: Medium (expected, will fix in Week 14-15 with DI container)

**Policy**: Services should not directly import from each other. Service dependencies should be injected via a Dependency Injection (DI) container.

**Current Violations:**

1. **knowledge_search_service → embedding_service** (line 25)
   - Violation: Direct import of embedding service
   - Impact: Tight coupling between knowledge and embedding services
   - Fix: Inject EmbeddingService via DI container

2. **knowledge_search_service → vector_store_service** (line 26)
   - Violation: Direct import of vector store service
   - Impact: Tight coupling between knowledge and vector store
   - Fix: Inject VectorStoreService via DI container

3. **user_service → auth_service** (line 41)
   - Violation: Direct import of auth service
   - Impact: User service coupled to auth implementation
   - Fix: Inject AuthService via DI container

4. **agent_orchestration_service → investigation_session_service** (line 30)
   - Violation: Direct import of session service
   - Impact: Agent orchestration coupled to session management
   - Fix: Inject InvestigationSessionService via DI container

5. **evidence_artifact_service → file_storage_service** (line 23)
   - Violation: Direct import of file storage service
   - Impact: Evidence service coupled to storage implementation
   - Fix: Inject FileStorageService via DI container

6. **agent_orchestration_service → evidence_artifact_service** (2 import chains)
   - Violation: Direct import of evidence service
   - Impact: Agent orchestration coupled to evidence management
   - Fix: Inject EvidenceArtifactService via DI container

**Analysis:**
- All violations are service-to-service dependencies
- These are **expected** and represent current service factory pattern
- Will be resolved in **Phase 3, Week 14-15** when DI container is implemented
- Pattern: Higher-level services (orchestration, knowledge) depend on lower-level services (storage, auth)

---

### Contract 2: Services Cannot Import API Layer ✅ KEPT

**Status**: 0 violations
**Severity**: Critical (any violation blocks merge)

**Policy**: Service layer must not import from API layer. API depends on services, not vice versa.

**Result**: **PERFECT COMPLIANCE** ✅

This is a critical architectural boundary that prevents circular dependencies between layers.

---

### Contract 3: Models Cannot Import Services ✅ KEPT

**Status**: 0 violations
**Severity**: Critical (any violation blocks merge)

**Policy**: Model classes (data structures, DTOs, entities) must not import service layer. This prevents circular dependencies.

**Result**: **PERFECT COMPLIANCE** ✅

Models are properly isolated as data structures without business logic dependencies.

---

## Violation Analysis Summary

### By Contract

| Contract | Violations | Status | Fix Timeline |
|----------|-----------|--------|--------------|
| Service Independence | 6 | BROKEN | Week 14-15 (DI Container) |
| Services → API (Forbidden) | 0 | KEPT ✅ | Maintained |
| Models → Services (Forbidden) | 0 | KEPT ✅ | Maintained |

### By Severity

| Severity | Count | Contracts |
|----------|-------|-----------|
| Critical | 0 | None (all critical contracts kept) |
| Medium | 6 | Service independence |
| Low | 0 | None |

### By File (Offenders)

| File | Violations | Contract |
|------|-----------|----------|
| knowledge_search_service.py | 2 | Service independence |
| agent_orchestration_service.py | 2 | Service independence |
| user_service.py | 1 | Service independence |
| evidence_artifact_service.py | 1 | Service independence |

---

## Quick Win Opportunities

**Target**: Reduce violations by 20-30% (1-2 violations fixed)

### Quick Win #1: file_storage_service injection
- **Current**: `evidence_artifact_service.py:23` directly imports `file_storage_service`
- **Fix**: Already using service_factory pattern; can wire through DI when container is ready
- **Effort**: Low (already abstracted)
- **Impact**: -1 violation (16% reduction)

### Quick Win #2: auth_service injection
- **Current**: `user_service.py:41` directly imports `auth_service`
- **Fix**: Use service_factory or DI injection
- **Effort**: Low
- **Impact**: -1 violation (16% reduction)

**Note**: Given that all violations follow the same pattern (service-to-service imports that need DI), it's more efficient to fix ALL 6 violations together when implementing the DI container in Week 14-15 rather than doing piecemeal fixes now.

**Decision**: **Defer all fixes to Week 14-15** for consistency and efficiency.

---

## Policy: Zero New Violations

### Enforcement

**CI/CD Integration**: Import-linter runs on every pull request via `.github/workflows/ci-cd.yml`

**Policy**:
1. **Zero new violations allowed**: PRs that introduce new violations will be blocked
2. **Existing violations tracked**: Current 6 violations are documented and accepted as technical debt
3. **Contract violations failing CI**: Any violation of Contract 2 or Contract 3 blocks merge immediately
4. **Service independence violations**: New service-to-service imports must go through DI container (after Week 14-15)

### Violation Baseline Check

The `scripts/check_import_violations.py` script compares current violations against this baseline:

- **Expected violations**: 6 (service independence)
- **Expected clean contracts**: Services → API, Models → Services
- **Fails if**: New violations detected or clean contracts broken

---

## Remediation Plan

### Week 13 (Current) ✅
- [x] Install and configure import-linter
- [x] Document baseline violations
- [x] Enable CI/CD enforcement
- [x] Zero new violations policy in effect

### Week 14-15: DI Container Implementation
- [ ] Implement Dependency Injection container
- [ ] Refactor service_factory to use DI
- [ ] Inject EmbeddingService into KnowledgeSearchService
- [ ] Inject VectorStoreService into KnowledgeSearchService
- [ ] Inject AuthService into UserService
- [ ] Inject InvestigationSessionService into AgentOrchestrationService
- [ ] Inject FileStorageService into EvidenceArtifactService
- [ ] Inject EvidenceArtifactService into AgentOrchestrationService
- [ ] **Target**: Contract 1 violations = 0

### Week 16-18: Vertical Slice Extraction
- [ ] Expand import-linter contracts for module boundaries
- [ ] Add independence contracts for modules/auth, modules/case, etc.
- [ ] Enforce module-to-module communication patterns
- [ ] Prevent cross-module imports (except via shared interfaces)

---

## Technical Details

### Files Analyzed
- **Total files**: 262
- **Total dependencies**: 614
- **Services scanned**: 10 (auth, case, investigation_session, knowledge_search, evidence_artifact, user, embedding, vector_store, file_storage, agent_orchestration)

### Import-Linter Configuration
- **Config file**: `.importlinter`
- **Root package**: `faultmaven`
- **Contracts**: 3 (service independence, forbidden API imports, forbidden service imports from models)

### Contract Types Used
- **Independence**: Services should not import each other
- **Forbidden**: Explicit module-to-module import bans

---

## Appendix: Full Violation Details

### Violation 1: knowledge_search_service → embedding_service
```python
# File: faultmaven/services/knowledge_search_service.py:25
from faultmaven.services.embedding_service import EmbeddingService
```
**Fix**: Inject via DI container in Week 14-15

### Violation 2: knowledge_search_service → vector_store_service
```python
# File: faultmaven/services/knowledge_search_service.py:26
from faultmaven.services.vector_store_service import VectorStoreService
```
**Fix**: Inject via DI container in Week 14-15

### Violation 3: user_service → auth_service
```python
# File: faultmaven/services/user_service.py:41
from faultmaven.services.auth_service import AuthenticationError, AuthService
```
**Fix**: Inject via DI container in Week 14-15

### Violation 4: agent_orchestration_service → investigation_session_service
```python
# File: faultmaven/services/agent_orchestration_service.py:30
from faultmaven.services.investigation_session_service import APIInvestigationSessionService
```
**Fix**: Inject via DI container in Week 14-15

### Violation 5: evidence_artifact_service → file_storage_service
```python
# File: faultmaven/services/evidence_artifact_service.py:23
from faultmaven.services.file_storage_service import FileStorageService
```
**Fix**: Inject via DI container in Week 14-15

### Violation 6: agent_orchestration_service → evidence_artifact_service
```python
# File: faultmaven/services/agent_orchestration_service.py:31
# Direct import + transitive via agent_tools
from faultmaven.services.evidence_artifact_service import APIEvidenceArtifactService
```
**Fix**: Inject via DI container in Week 14-15

---

## Conclusion

Import-linter baseline established successfully. The codebase has **strong architectural boundaries** with only expected service-to-service coupling violations that will be resolved systematically in Week 14-15.

**Key Achievements:**
- ✅ Zero violations on critical contracts (API isolation, Model isolation)
- ✅ CI/CD enforcement prevents new violations
- ✅ Clear remediation path (DI container in Week 14-15)
- ✅ Baseline documented for tracking progress

**Next Steps:**
1. Merge this PR to establish enforcement
2. Proceed to Week 14-15: Deployment Profiles & DI Container
3. Re-run import-linter after DI implementation to verify Contract 1 compliance
