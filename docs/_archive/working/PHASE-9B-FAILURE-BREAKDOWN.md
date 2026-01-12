# Phase 9B: Failure Breakdown Table

**Date**: 2026-01-10
**Baseline**: 179 failures, 416 passing (69.2%), 6 errors

---

## Summary by Category

| Category | Tests | Effort | Priority | Expected Gain |
|----------|-------|--------|----------|---------------|
| **Quick Fixes** | 55 | 2-4 hrs | HIGH | +55-70 passing |
| **Delete Obsolete** | 55 | 1-2 hrs | MEDIUM | Clean up suite |
| **Complex Fixes** | 49 | 4-8 hrs | MEDIUM | +20-30 passing |
| **Out of Scope** | 16 | N/A | LOW | Defer |
| **Fixed (Production Bug)** | 15 errors | 0 hrs | CRITICAL | ✅ DONE |

---

## Detailed Breakdown by File

| # | File | Failures | Root Cause | Error Type | Category | Action | Priority | Est. Time |
|---|------|----------|------------|------------|----------|--------|----------|-----------|
| 1 | `test_evidence_api.py` | 28 | Route mismatch | 404 Not Found | **DELETE** | Delete file | P1 | 30 min |
| 2 | `test_cases_api.py` | 24 | Mock config | 500 Server Error | **QUICK FIX** | Fix mocks | P1 | 1 hr |
| 3 | `test_agent_execution_integration.py` | 21 | SQLAlchemy async | Async context | **COMPLEX** | Fix eager load | P2 | 2 hrs |
| 4 | `test_users_api.py` | 21 | Helper bug | KeyError | **QUICK FIX** | Fix helper | P1 | 30 min |
| 5 | `test_new_architecture_workflows.py` | 19 | Missing attr | AttributeError | **DELETE?** | Evaluate | P2 | 1 hr |
| 6 | `test_organization_authorization.py` | 15 | Auth logic | Wrong status | **SECURITY** | Fix auth | P0 | 2-3 hrs |
| 7 | `test_agent_api_integration.py` | 13 | Unknown | 500 internal | **COMPLEX** | Debug | P2 | 2-3 hrs |
| 8 | `test_alembic_migrations.py` | 10 | Missing cmd | Command not found | **QUICK FIX** | Fix PATH | P3 | 30 min |
| 9 | `test_sessions_api.py` | 6 | Mock/logic | Wrong status | **QUICK FIX** | Investigate | P2 | 1 hr |
| 10 | `test_architectural_compliance.py` | 8 | Missing attr | AttributeError | **DELETE?** | Evaluate | P2 | 30 min |
| 11 | `test_session_case_integration.py` | 5 | Integration | Various | **OUT OF SCOPE** | Defer | P3 | N/A |
| 12 | `test_case_service_integration.py` | 5 | Integration | Various | **OUT OF SCOPE** | Defer | P3 | N/A |
| 13 | `test_protection_integration.py` | 4 | Missing feature | Various | **OUT OF SCOPE** | Defer | P3 | N/A |
| 14 | `test_investigation_session_service_integration.py` | 2 | Minor issues | Various | **OUT OF SCOPE** | Defer | P3 | N/A |
| 15 | `test_mock_verification.py` | 1 | Minor | Assertion | **QUICK FIX** | Fix | P3 | 15 min |
| 16 | `test_main_app.py` | 1 | Minor | Assertion | **QUICK FIX** | Fix | P3 | 15 min |
| 17 | `test_kb_ingestion_and_indexing.py` | 1 | Minor | Assertion | **OUT OF SCOPE** | Defer | P3 | N/A |
| 18 | `test_investigation_session_integration.py` | 1 | Minor | Assertion | **OUT OF SCOPE** | Defer | P3 | N/A |

**Total**: 185 (179 failures + 6 errors)

---

## Error Pattern Summary

### Error Types Distribution

| Error Type | Count | Examples |
|------------|-------|----------|
| **404 Not Found** | 28 | Evidence API route mismatch |
| **500 Internal Server Error** | 37+ | Cases API mocks, Agent API, Sessions |
| **Wrong Status Code** | 30+ | Auth (200 vs 403), Sessions (422 vs 400) |
| **Async Context Error** | 21 | SQLAlchemy greenlet_spawn |
| **AttributeError** | 27 | Missing container attrs, DIContainer |
| **KeyError** | 21+ | Test helper (access_token) |
| **Command Not Found** | 10 | Alembic PATH |
| **Other** | 11+ | Misc assertions, data issues |

---

## Phase 9B Roadmap

### Phase 9B-1: Quick Wins (2-4 hours)

| Task | Files | Tests | Status |
|------|-------|-------|--------|
| Fix Cases API mocks | `test_cases_api.py` | 24 | TODO |
| Fix Users API helper | `test_users_api.py` | 21 | TODO |
| Fix Alembic PATH | `test_alembic_migrations.py` | 10 | TODO |
| Fix minor issues | `test_mock_verification.py`, `test_main_app.py` | 2 | TODO |

**Target**: +55-70 passing tests → **471-486 passing (78-81%)**

### Phase 9B-2: Delete Obsolete (1-2 hours)

| Task | Files | Tests | Status |
|------|-------|-------|--------|
| Delete Evidence API tests | `test_evidence_api.py` | 28 | TODO |
| Evaluate Architecture tests | `test_new_architecture_workflows.py` | 19 | TODO |
| Evaluate Compliance tests | `test_architectural_compliance.py` | 8 | TODO |

**Target**: Cleaner test suite → **~470-480 passing (~85-90%)**

### Phase 9B-3: Complex Fixes (4-8 hours)

| Task | Files | Tests | Status |
|------|-------|-------|--------|
| Fix Authorization logic | `test_organization_authorization.py` | 15 | TODO - SECURITY |
| Fix SQLAlchemy async | `test_agent_execution_integration.py` | 21 | TODO |
| Debug Agent API | `test_agent_api_integration.py` | 13 | TODO |
| Fix Sessions API | `test_sessions_api.py` | 6 | TODO |

**Target**: +20-30 passing tests → **491-516 passing (85-89%)**

---

## Success Metrics

### Baseline (Post Production Bug Fix)
- **Passing**: 416 tests (69.2%)
- **Failing**: 179 tests
- **Errors**: 6 tests
- **Total**: 601 tests

### Phase 9B Goal
- **Passing**: 500+ tests (83%+ pass rate) ✅
- **Failing**: <100 tests
- **Errors**: 0 tests

### Stretch Goal
- **Passing**: 516+ tests (89%+ pass rate)
- **Failing**: <50 tests
- **Total**: Reduced to ~550 tests (after deletions)

---

## Risk Matrix

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Mock fixes take longer than expected | MEDIUM | LOW | Start with one test, verify pattern |
| Evidence deletion controversial | LOW | MEDIUM | Document rationale, get approval |
| SQLAlchemy fix reveals schema issues | MEDIUM | HIGH | Test in isolation first |
| Auth fix breaks legitimate access | LOW | HIGH | Review passing auth tests first |
| Agent API needs major refactor | MEDIUM | HIGH | Time-box to 2 hours |

---

## Key Insights from Analysis

### What Worked from Phase 9A

1. **Evaluation-First Deletion** ✅
   - Don't blindly delete - investigate first
   - Document rationale clearly
   - Get approval if controversial

2. **`dependency_overrides` Pattern** ✅
   - Better than `@patch` for FastAPI mocking
   - Fixes 67 tests in Phase 9A with auth pattern
   - Use for Cases API mock fixes

3. **Production Bug Fixes Have High Impact** ✅
   - Fixing RedisSessionStore fixed many tests
   - Fixing agent_orchestration_service unblocked 15 collection errors
   - Always check production code first

### New Patterns Discovered

1. **Route/Test Mismatches**
   - Tests written for deprecated API design
   - Production implementation is correct
   - Solution: Delete obsolete tests

2. **SQLAlchemy Async Context**
   - Lazy loading fails in tests
   - Need eager loading or better fixtures
   - Common pattern in integration tests

3. **Test Helper Bugs**
   - Shared helpers (`register_and_login`) break many tests
   - Fix once, gain many tests
   - High leverage fixes

---

## Next Steps

1. **Review this analysis** with team
2. **Get approval** for Evidence API deletion
3. **Start Phase 9B-1** (Quick Wins)
4. **Create separate ticket** for authorization security fix
5. **Re-run analysis** after each phase

---

## Appendix: Files to Investigate

### Need Detailed Review

1. **test_new_architecture_workflows.py** (19 failures)
   - Check if testing current or deprecated architecture
   - Look for `LLMRouter` usage
   - Decision: Delete or update?

2. **test_architectural_compliance.py** (8 tests)
   - Check if `DIContainer.case_service` should exist
   - May need to update DI access pattern
   - Decision: Fix or delete?

3. **test_sessions_api.py** (6 failures)
   - Mixed error types (500, 422, 404)
   - Need to investigate each failure
   - Likely mock or logic issues

4. **test_agent_api_integration.py** (13 failures)
   - All return "internal_error"
   - Need to add debug logging
   - May uncover deeper issues

### Can Skip for Now

- `test_session_case_integration.py` (5 failures) - Out of scope
- `test_case_service_integration.py` (5 failures) - Out of scope
- `test_protection_integration.py` (4 failures) - Out of scope
- `test_investigation_session_service_integration.py` (2 failures) - Out of scope
- `test_kb_ingestion_and_indexing.py` (1 failure) - Out of scope
- `test_investigation_session_integration.py` (1 failure) - Out of scope
