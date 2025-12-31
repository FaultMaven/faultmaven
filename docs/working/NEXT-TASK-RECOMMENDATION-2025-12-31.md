# Next Task Recommendation: TASK-026 Hypothesis & Solution Tracking

## Executive Summary

**Date**: 2025-12-31
**Current Status**: PR #27 (TASK-024: Report Module) MERGED ✅
**Recommendation**: Proceed with **TASK-026: Hypothesis & Solution Tracking**
**Strategic Decision**: SKIP TASK-025 (Evidence Download & Token Refresh already exist)

---

## Current State

### Completed Work (Phase 0 + Phase 1 Week 1-4)

| Task | PR | Status | Achievement |
|------|----|----|-------------|
| TASK-017 | #20 | ✅ MERGED | JWT Authentication API (26 tests) |
| TASK-019 | #22 | ✅ MERGED | Admin User Management API (126 tests) |
| TASK-020 | N/A | ✅ COMPLETE | Remove Legacy Header Authentication |
| TASK-021 | #23 | ✅ MERGED | Organization Management API (142 tests) |
| TASK-023 | #26 | ✅ MERGED | TenantProvider (35 tests, deployment neutrality) |
| **TASK-024** | **#27** | **✅ MERGED** | **Report Module (47 tests, 7 CRITICAL endpoints)** |

**Total Tests**: 376 tests passing
**Coverage**: 90%+ on all new modules
**Timeline**: On schedule (Phase 1 Week 4 complete)

---

## Strategic Analysis: TASK-025 vs TASK-026

### TASK-025: Evidence Download & Token Refresh (SKIP ⏩)

**Status**: Both endpoints ALREADY EXIST ✅

**Evidence Download**:
- **Path**: `GET /api/v1/cases/{case_id}/evidence/{evidence_id}/download`
- **File**: `/home/swhouse/product/faultmaven/faultmaven/api/routes/evidence.py:162`
- **Features**: JWT auth, streaming, multi-tenant isolation, proper headers
- **Production Ready**: YES

**Token Refresh**:
- **Path**: `POST /api/v1/auth/refresh`
- **File**: `/home/swhouse/product/faultmaven/faultmaven/api/routes/auth.py:598`
- **Features**: Token rotation, JWT validation, revocation handling
- **Production Ready**: YES

**Decision**: SKIP TASK-025 implementation (saves 1 week)

**Action Required**: Verify test coverage for both endpoints
```bash
pytest tests/api/routes/test_evidence.py -k download -v
pytest tests/api/routes/test_auth.py -k refresh -v
```

---

### TASK-026: Hypothesis & Solution Tracking (NEXT PRIORITY ✅)

**Why This is the Correct Next Task**:

1. **Core Troubleshooting Workflow**
   - Enables systematic hypothesis-driven investigation
   - Links evidence → hypothesis → solution → resolution
   - Completes the investigation methodology

2. **Business Value**
   - Users can track multiple investigation theories
   - Confidence scoring guides investigation priority
   - Solution documentation feeds knowledge base
   - Compliance: Formal investigation trail for audits

3. **Technical Foundation**
   - Establishes investigation orchestration pattern
   - Demonstrates agent integration at scale
   - Tests multi-tenant isolation for complex workflows
   - Builds on TenantProvider (TASK-023) and Report Module (TASK-024)

4. **Strategic Position**
   - 2nd batch of CRITICAL endpoints (3 endpoints + 6 supporting)
   - After TASK-026: 10 of 15 CRITICAL endpoints delivered
   - Accelerates Phase 1 completion (Week 6 vs Week 7)

---

## TASK-026 Specification Summary

### Objectives

**Implement 3 CRITICAL endpoints**:
1. `POST /api/v1/cases/{case_id}/hypotheses` - Track investigation hypotheses
2. `PUT /api/v1/hypotheses/{id}` - Update hypothesis status (testing → confirmed/rejected)
3. `POST /api/v1/cases/{case_id}/solutions` - Document solutions

**Plus 6 supporting endpoints** for complete CRUD:
- `GET /api/v1/cases/{case_id}/hypotheses` - List hypotheses
- `GET /api/v1/hypotheses/{id}` - Get hypothesis
- `DELETE /api/v1/hypotheses/{id}` - Delete hypothesis
- `GET /api/v1/cases/{case_id}/solutions` - List solutions
- `PUT /api/v1/solutions/{id}` - Update solution
- `POST /api/v1/hypotheses/{id}/validate` - Validate with confidence scoring

---

### Architecture Design

**Key Component**: **Investigation Orchestrator**

```
┌─────────────┐
│   Client    │
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│ Hypothesis API  │
└──────┬──────────┘
       │
       ▼
┌──────────────────────────────┐
│ InvestigationOrchestrator    │ ◄── NEW ORCHESTRATION LAYER
│  - generate_hypotheses()     │
│  - validate_hypothesis()     │
│  - link_solution()           │
│  - get_progress()            │
└──┬────────────────┬──────────┘
   │                │
   ▼                ▼
┌────────────┐  ┌────────────┐
│ AgentMgr   │  │ CaseService│
│ (optional) │  │            │
└────────────┘  └────────────┘
```

**Design Decision**: Orchestrator sits BETWEEN Case API and Agent framework
- Agents generate hypotheses (reasoning)
- Orchestrator manages state (business logic)
- Clear separation of concerns

---

### Database Schema

**New Tables**:
1. **hypotheses**
   - `id`, `case_id`, `organization_id`
   - `description`, `status` (testing/confirmed/rejected)
   - `confidence_level` (0.00-1.00)
   - `evidence_refs` (JSONB array)
   - `findings` (JSONB object)
   - `source` (manual/ai_generated)
   - Audit trail (created_by, created_at, updated_at)

2. **solutions**
   - `id`, `case_id`, `hypothesis_id` (optional link)
   - `title`, `description`, `steps` (JSONB array)
   - `validation` (JSONB object)
   - `implemented`, `effectiveness_score`
   - `knowledge_article_id` (future KB integration)
   - Audit trail

**Multi-Tenant Isolation**: Enforced via `organization_id` foreign key + indexes

---

### Implementation Timeline (2 Weeks)

**Day 1-3: Database & Repository Layer**
- Alembic migration (`20250101_add_hypotheses_solutions.py`)
- SQLAlchemy models (Hypothesis, Solution)
- Repositories (HypothesisRepository, SolutionRepository)
- 12 repository tests

**Day 4-6: Service Layer (Investigation Orchestrator)**
- `InvestigationOrchestrator` class
- Hypothesis lifecycle methods (generate, validate, link)
- Agent integration (stubbed initially)
- 8 orchestrator tests

**Day 7-8: API Endpoints**
- `hypotheses.py` router (9 endpoints)
- TenantProvider integration
- JWT authentication
- 10 API integration tests

**Day 9-10: E2E Workflows & Documentation**
- 3 E2E workflow tests
- OpenAPI documentation
- Usage examples
- PR review and merge

**Total**: 10 working days, 33+ tests, 90%+ coverage

---

### Test Requirements

| Test Type | File | Count | Coverage Target |
|-----------|------|-------|----------------|
| Repository | `test_hypothesis_repository.py` | 6 | 95%+ |
| Repository | `test_solution_repository.py` | 6 | 95%+ |
| Service | `test_investigation_orchestrator.py` | 8 | 90%+ |
| API | `test_hypotheses.py` | 10 | 90%+ |
| E2E | `test_investigation_workflow.py` | 3 | 80%+ |
| **Total** | | **33+** | **90%+** |

---

## Deliverables

### Code Files (8 new files)

1. **Migration**: `alembic/versions/20250101_add_hypotheses_solutions.py`
2. **Models**: `faultmaven/models/hypothesis.py`
3. **Repositories**:
   - `faultmaven/repositories/hypothesis_repository.py`
   - `faultmaven/repositories/solution_repository.py`
4. **Service**: `faultmaven/services/domain/investigation_orchestrator.py`
5. **API**: `faultmaven/api/v1/routes/hypotheses.py`
6. **Tests** (5 test files):
   - `tests/repositories/test_hypothesis_repository.py`
   - `tests/repositories/test_solution_repository.py`
   - `tests/services/test_investigation_orchestrator.py`
   - `tests/api/v1/routes/test_hypotheses.py`
   - `tests/integration/test_investigation_workflow.py`

### Documentation Files (Created)

1. ✅ **Task Specification**: `/home/swhouse/product/faultmaven/docs/working/TASK-026-HYPOTHESIS-SOLUTION-TRACKING.md` (26,000 words, complete)
2. ✅ **TASK-025 Skip Analysis**: `/home/swhouse/product/faultmaven/docs/working/TASK-025-STRATEGIC-SKIP-ANALYSIS.md`
3. ✅ **This Recommendation**: `/home/swhouse/product/faultmaven/docs/working/NEXT-TASK-RECOMMENDATION-2025-12-31.md`

---

## Updated Phase 1 Timeline

### Original Plan (9 weeks)
- Week 1-4: Report Module (TASK-024) - ✅ COMPLETE
- Week 5: Evidence Download & Token Refresh (TASK-025)
- Week 6-7: Hypothesis & Solution Tracking (TASK-026)
- Week 8-9: Session Messages & Agent Chat (TASK-027)

### Revised Timeline (8 weeks - 1 week acceleration)
- Week 1-4: Report Module (TASK-024) - ✅ COMPLETE
- ~~Week 5: TASK-025~~ - ⏩ SKIPPED (endpoints exist)
- **Week 5-6: Hypothesis & Solution Tracking (TASK-026)** - 🟡 **NEXT** (starts now)
- Week 7-8: Session Messages & Agent Chat (TASK-027)

**Impact**:
- 15 CRITICAL endpoints → 13 endpoints to implement (2 already exist)
- Phase 1: 9 weeks → 8 weeks (1-week acceleration)
- Tests: 128 new tests → 115 new tests needed

---

## Success Metrics

### TASK-026 Acceptance Criteria

| Metric | Target | Status |
|--------|--------|--------|
| **Endpoints Implemented** | 9 (3 CRITICAL + 6 supporting) | 🟡 Pending |
| **Tests Passing** | 33+ | 🟡 Pending |
| **Test Coverage** | 90%+ | 🟡 Pending |
| **Multi-Tenant Isolation** | Enforced | 🟡 Pending |
| **Deployment Neutrality** | TenantProvider | 🟡 Pending |
| **Timeline** | 2 weeks (10 days) | 🟡 Starting |

### Phase 1 Progress (After TASK-026)

| Metric | Current | After TASK-026 | Target |
|--------|---------|----------------|--------|
| **CRITICAL Endpoints** | 7/15 (47%) | 10/15 (67%) | 15/15 (100%) |
| **Total Tests** | 376 | 409+ (376 + 33) | 491+ |
| **Weeks Elapsed** | 4/8 | 6/8 | 8/8 |
| **Phase Completion** | 50% | 75% | 100% |

---

## Risk Assessment

### TASK-026 Risks (LOW)

1. **Agent Integration Complexity** - MITIGATED
   - Stub agent initially (`agent_manager=None`)
   - Users create hypotheses manually
   - AI integration added incrementally later

2. **Database Migration Issues** - MITIGATED
   - Test on local SQLite first
   - Test on PostgreSQL in Docker
   - Verify rollback capability

3. **Timeline Overrun** - MITIGATED
   - Start with simple orchestrator (no AI)
   - Focus on CRUD operations first
   - Daily progress check-ins

---

## Recommendations

### Immediate Actions (Today)

1. ✅ **Approve TASK-026 Specification**
   - Review `/home/swhouse/product/faultmaven/docs/working/TASK-026-HYPOTHESIS-SOLUTION-TRACKING.md`
   - Confirm 2-week timeline acceptable
   - Assign Backend Engineer + AI Specialist

2. ✅ **Verify TASK-025 Endpoints** (Optional)
   ```bash
   # Test evidence download
   curl -X GET "http://localhost:8000/api/v1/cases/CASE-123/evidence/EV-456/download" \
     -H "Authorization: Bearer <token>"

   # Test token refresh
   curl -X POST "http://localhost:8000/api/v1/auth/refresh" \
     -H "Content-Type: application/json" \
     -d '{"refresh_token": "..."}'
   ```

3. ✅ **Create Feature Branch**
   ```bash
   cd /home/swhouse/product/faultmaven
   git checkout -b claude/hypothesis-solution-tracking-TASK026
   ```

### Week 5-6 Execution Plan

**Day 1** (Today):
- Create Alembic migration
- Implement Hypothesis and Solution SQLAlchemy models
- Run migration: `alembic upgrade head`

**Day 2-3**:
- Implement HypothesisRepository and SolutionRepository
- Write 12 repository tests
- Verify all tests passing

**Day 4-6**:
- Implement InvestigationOrchestrator
- Wire into DI container
- Write 8 orchestrator tests

**Day 7-8**:
- Implement 9 API endpoints in `hypotheses.py`
- Write 10 API integration tests
- Test with Postman/httpie

**Day 9-10**:
- Write 3 E2E workflow tests
- Update OpenAPI documentation
- Create PR and merge

---

## Next Task After TASK-026

**TASK-027: Session Messages & Agent Chat** (3 CRITICAL endpoints)
- `GET /api/v1/sessions/{id}/messages` - Conversation history
- `POST /api/v1/sessions/{id}/messages` - Add message
- `POST /api/v1/agent/chat` - Agent chat endpoint (streaming)

**Timeline**: Week 7-8 (2 weeks)
**Tests**: 30+ tests
**Features**: SSE/WebSocket streaming, context management, message persistence

---

## Conclusion

**Phase 1 Status**: Week 4 of 8 COMPLETE ✅

After PR #27 (TASK-024: Report Module), we have successfully delivered:
- 7 CRITICAL endpoints (Report Module)
- 376 tests passing
- 90%+ coverage
- Deployment-neutral architecture

**Strategic Decision**: SKIP TASK-025 (Evidence Download & Token Refresh)
- Both endpoints already exist with JWT auth
- Saves 1 week of development
- Accelerates Phase 1 completion

**Next Priority**: **TASK-026 - Hypothesis & Solution Tracking**
- 9 endpoints (3 CRITICAL + 6 supporting)
- Investigation orchestrator for hypothesis lifecycle
- 33+ tests, 90%+ coverage
- 2-week timeline (Days 1-10)

**Recommendation**: **APPROVE and BEGIN TASK-026 IMMEDIATELY**

The task specification is complete, comprehensive, and ready for implementation. All dependencies are met (TASK-023 TenantProvider, TASK-024 Report Module). The architecture is well-defined with clear separation of concerns.

**After TASK-026**: 10 of 15 CRITICAL endpoints delivered (67% complete)

---

**Document Metadata**:
- **Created**: 2025-12-31
- **Author**: Solutions Architect
- **Version**: 1.0
- **Status**: RECOMMENDATION FOR APPROVAL
- **Decision Required**: Approve TASK-026 execution

**Related Documents**:
- `/home/swhouse/product/faultmaven/docs/working/TASK-026-HYPOTHESIS-SOLUTION-TRACKING.md` (complete specification)
- `/home/swhouse/product/faultmaven/docs/working/TASK-025-STRATEGIC-SKIP-ANALYSIS.md` (skip justification)
- `/home/swhouse/product/faultmaven/docs/FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md` (strategic plan)
- `/home/swhouse/product/faultmaven/docs/working/PHASE-0-COMPLETION-AND-NEXT-STEPS.md` (Phase 0 summary)
