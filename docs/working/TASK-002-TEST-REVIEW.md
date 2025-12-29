# TASK-002-TEST-REVIEW: Test-Engineer Review & Execution

## Task Metadata
- **Phase**: Week 1, Day 2-3 (Foundation - Test Review)
- **Priority**: P0 (Blocks TASK-002 approval)
- **Estimated Time**: 1-2 hours
- **Dependencies**: TASK-002 (Developer submits PR with tests)
- **Assignee**: Test-Engineer
- **Reports To**: Solutions Architect

## Objective

**Run tests, review test code quality, and verify coverage** for TASK-002 (Case Repository Refactoring):

1. **RUN all tests** and verify they pass
2. **RUN coverage analysis** and verify ≥80%
3. **REVIEW test code quality** (edge cases, assertions, proper patterns)
4. **IDENTIFY missing test scenarios**
5. **SIGN OFF** when criteria met

## Context

The developer implemented `DatabaseCaseRepository` and wrote unit + integration tests. Your job is to ensure the tests themselves are production-quality before solutions-architect final approval.

## Review Criteria

### 1. Coverage Analysis ✅ MANDATORY

**Requirement:** ≥80% coverage for all new code

**Check:**
```bash
# Run coverage report
pytest --cov=faultmaven/infrastructure/persistence --cov-report=term-missing

# Verify coverage threshold
pytest --cov=faultmaven/infrastructure/persistence --cov-fail-under=80
```

**Review:**
- [ ] Overall coverage ≥ 80%
- [ ] All CRUD methods have tests
- [ ] All error paths have tests
- [ ] No untested critical code paths

**If coverage < 80%:** Request additional tests in PR review

---

### 2. Unit Test Quality Review

**File to review:** `tests/unit/infrastructure/persistence/test_database_case_repository.py`

#### Required Test Scenarios
- [ ] `test_create_case` - Happy path case creation
- [ ] `test_create_case_duplicate_id` - Error handling for duplicate ID
- [ ] `test_get_case` - Retrieve existing case
- [ ] `test_get_case_not_found` - Return None for non-existent case
- [ ] `test_update_case` - Update case fields
- [ ] `test_update_case_not_found` - Error when updating non-existent case
- [ ] `test_delete_case` - Delete existing case
- [ ] `test_delete_case_not_found` - Handle delete of non-existent case
- [ ] `test_list_cases_by_user` - List all cases for user
- [ ] `test_list_cases_empty` - Return empty list when no cases
- [ ] `test_status_transition` - Track status changes
- [ ] `test_concurrent_updates` - Test async safety
- [ ] `test_rollback_on_error` - Transaction rollback on exception

#### Test Quality Checklist
- [ ] **Isolation:** Each test is independent (no shared state)
- [ ] **Clarity:** Test names describe what they test
- [ ] **Assertions:** Meaningful assertions (not just "assert result")
- [ ] **Edge cases:** Boundary conditions tested (empty strings, None, etc.)
- [ ] **Mocking:** Proper use of mocks/fixtures (database session mocked in unit tests)
- [ ] **Async/await:** All async functions properly awaited
- [ ] **Cleanup:** Proper teardown (no test data leaks)

#### Test Anti-Patterns to Flag
- ❌ Tests that depend on execution order
- ❌ Tests with hardcoded timestamps/IDs that could fail randomly
- ❌ Tests that sleep/wait instead of using proper async patterns
- ❌ Tests without assertions (just checking "doesn't crash")
- ❌ Tests that use real database in unit tests (should use mocks)
- ❌ Multiple unrelated assertions in one test (should split)

---

### 3. Integration Test Quality Review

**File to review:** `tests/integration/test_case_repository_integration.py`

#### Required Integration Scenarios
- [ ] `test_full_case_lifecycle` - Create → Read → Update → Delete flow
- [ ] `test_case_with_evidence` - Case with linked evidence records
- [ ] `test_case_with_hypotheses` - Case with linked hypotheses
- [ ] `test_case_with_solutions` - Case with linked solutions
- [ ] `test_repository_factory` - Factory returns correct implementation
- [ ] `test_concurrent_case_creation` - Multiple users creating cases simultaneously
- [ ] `test_database_constraint_violations` - Foreign key violations handled

#### Integration Test Checklist
- [ ] **Real database:** Uses test SQLite database (not mocks)
- [ ] **Isolation:** Each test uses fresh database or transaction rollback
- [ ] **Cleanup:** Database cleaned after each test
- [ ] **Fixtures:** Proper use of pytest fixtures for DB setup
- [ ] **Realistic data:** Tests use realistic case data (not minimal stubs)
- [ ] **Foreign keys:** Tests verify foreign key relationships work
- [ ] **Transactions:** Tests verify transaction boundaries

---

### 4. Test Fixtures and Configuration Review

**File to review:** `tests/conftest.py`

#### Required Fixtures
- [ ] `db_session` - Provides test database session
- [ ] `case_repository` - Provides repository instance for testing
- [ ] `sample_case` - Factory for creating test cases
- [ ] Database cleanup (autouse fixture or explicit cleanup)

#### Configuration Checklist
- [ ] Test database URL configured (not production DB!)
- [ ] Fixtures properly scoped (function/module/session)
- [ ] Async fixtures use `@pytest_asyncio.fixture`
- [ ] No hardcoded credentials or secrets

---

### 5. CI/CD Integration Review

**Check GitHub Actions workflow**

- [ ] All tests run in CI/CD
- [ ] Coverage report generated
- [ ] Coverage threshold enforced (--cov-fail-under=80)
- [ ] Tests run on multiple Python versions (if applicable)
- [ ] Test database properly set up in CI environment

---

### 6. Missing Test Scenarios (Gap Analysis)

Look for these common missing tests:

#### Error Handling
- [ ] Database connection failure
- [ ] Malformed case data
- [ ] Invalid case_id format
- [ ] Constraint violations (unique, foreign key, not null)
- [ ] Transaction deadlock/timeout

#### Edge Cases
- [ ] Very long case titles (200 char limit)
- [ ] Empty JSONB fields
- [ ] Case with no evidence/hypotheses/solutions
- [ ] Null vs empty string handling
- [ ] Unicode characters in case data

#### Performance/Async
- [ ] Multiple simultaneous reads
- [ ] Multiple simultaneous writes to same case
- [ ] Repository handles async context correctly

---

## Deliverables

### 1. Coverage Report
```bash
# Generate and save coverage report
pytest --cov=faultmaven/infrastructure/persistence \
       --cov-report=html \
       --cov-report=term-missing > coverage_report.txt

# Check threshold
pytest --cov=faultmaven/infrastructure/persistence --cov-fail-under=80
```

### 2. Test Quality Assessment

Create: `docs/working/TASK-002-TEST-REVIEW-RESULTS.md`

**Template:**
```markdown
# Test Review Results: TASK-002

## Coverage Analysis
- Overall Coverage: X%
- Unit Test Coverage: X%
- Integration Test Coverage: X%
- ✅/❌ Meets 80% threshold

## Unit Test Quality
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Integration Test Quality
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Missing Test Scenarios
1. [scenario]
2. [scenario]

## Test Anti-Patterns Found
1. [anti-pattern description]

## Recommendations
1. [recommendation]
2. [recommendation]

## Final Assessment
- [ ] APPROVED - Tests are production-quality
- [ ] CHANGES REQUESTED - Tests need improvements
- [ ] REJECTED - Tests inadequate, need major rework

## Detailed Findings
[detailed review notes]
```

### 3. PR Review Comment

Post review to PR #X:
```markdown
## Test-Engineer Review: TASK-002

**Coverage:** X% (threshold: 80%) ✅/❌
**Unit Tests:** X tests - Quality: Good/Fair/Poor
**Integration Tests:** X tests - Quality: Good/Fair/Poor

### Issues Found
1. [issue with specific line reference]
2. [issue with specific line reference]

### Missing Tests
1. [missing scenario]
2. [missing scenario]

### Recommendations
1. [recommendation]

**Status:** ✅ APPROVED / ⚠️ CHANGES REQUESTED / ❌ NEEDS REWORK

See full review: docs/working/TASK-002-TEST-REVIEW-RESULTS.md
```

---

## Review Process

### Step 1: Checkout PR Branch
```bash
cd /home/swhouse/product/faultmaven
git fetch origin
git checkout pr-3  # or appropriate PR branch

# Install dependencies
pip install -e .
pip install -r requirements-test.txt
```

### Step 2: RUN TESTS (MANDATORY)
```bash
# Run all tests and verify they pass
pytest -v

# Run unit tests only
pytest tests/unit/infrastructure/persistence/ -v

# Run integration tests only
pytest tests/integration/test_case_repository_integration.py -v

# Check test count
pytest tests/unit/infrastructure/persistence/ --collect-only | grep "test session starts"
pytest tests/integration/test_case_repository_integration.py --collect-only | grep "test session starts"
```

**Expected:** All tests PASS (no failures, no errors)

### Step 3: RUN COVERAGE ANALYSIS (MANDATORY)
```bash
# Generate coverage report
pytest tests/unit/infrastructure/persistence/ \
       tests/integration/test_case_repository_integration.py \
       --cov=faultmaven/infrastructure/persistence/database_case_repository \
       --cov=faultmaven/infrastructure/persistence/database \
       --cov=faultmaven/infrastructure/persistence/models \
       --cov=faultmaven/infrastructure/persistence/repository_factory \
       --cov-report=term-missing \
       --cov-report=html

# Verify threshold
pytest tests/unit/infrastructure/persistence/ \
       tests/integration/test_case_repository_integration.py \
       --cov=faultmaven/infrastructure/persistence \
       --cov-fail-under=80
```

**Expected:** Coverage ≥80%

### Step 4: Open Coverage Report
```bash
# View detailed coverage in browser
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

**Verify:** No critical code paths untested

### Step 5: Review Test Code Quality
- Read test files (see quality criteria below)
- Check for anti-patterns
- Note missing scenarios

### Step 6: Document Findings
- Create TASK-002-TEST-REVIEW-RESULTS.md
- Document all findings
- Provide specific line references
- Give actionable recommendations

### Step 7: Submit Review
Post to PR with test results:

```markdown
## Test-Engineer Review: TASK-002

### Test Execution Results
- ✅/❌ All tests pass: X passed, Y failed
- ✅/❌ Coverage: X% (threshold: 80%)

### Test Quality Assessment
- Unit tests: X tests - Quality: Good/Fair/Poor
- Integration tests: X tests - Quality: Good/Fair/Poor

### Issues Found
1. [issue]

### Missing Tests
1. [missing scenario]

### Status
✅ APPROVED / ⚠️ CHANGES REQUESTED

See: docs/working/TASK-002-TEST-REVIEW-RESULTS.md
```

---

## Approval Criteria

### ✅ APPROVED if:
- Coverage ≥ 80%
- All required test scenarios present
- Test quality is good (clear, isolated, meaningful assertions)
- No major anti-patterns
- All tests pass locally and CI/CD
- Minor issues only (can be addressed in future)

### ⚠️ CHANGES REQUESTED if:
- Coverage 70-79% (close but needs improvement)
- Some test scenarios missing (not critical paths)
- Test quality fair (some unclear tests, minor anti-patterns)
- All tests pass but quality needs improvement

### ❌ NEEDS REWORK if:
- Coverage < 70%
- Critical test scenarios missing
- Major test anti-patterns (shared state, race conditions)
- Tests failing
- Poor test quality overall

---

## Test Quality Examples

### ✅ GOOD TEST
```python
@pytest.mark.unit
async def test_create_case_success():
    """Test creating a new case returns case with generated ID"""
    # Arrange
    repo = DatabaseCaseRepository(mock_session)
    case_data = {
        "user_id": "user_123",
        "title": "Test Case",
        "status": "consulting"
    }

    # Act
    result = await repo.create_case(case_data)

    # Assert
    assert result.case_id.startswith("case_")
    assert result.user_id == "user_123"
    assert result.title == "Test Case"
    assert result.status == "consulting"
    assert result.created_at is not None
```

### ❌ BAD TEST
```python
async def test_stuff():
    """Test case stuff"""
    repo = get_repo()
    case = repo.create_case({"title": "test"})
    assert case  # What does this verify?
```

**Issues:**
- No `@pytest.mark.unit` marker
- Name too vague ("stuff")
- Docstring unhelpful
- No Arrange/Act/Assert structure
- Missing async decorator
- Weak assertion (just checks truthy)
- Missing important assertions (case_id, created_at, etc.)

---

## Timeline

1. **Developer submits PR** with implementation + tests
2. **Test-engineer reviews** (1-2 hours)
3. **Test-engineer posts findings** to PR
4. If changes needed: **Developer updates tests**
5. If changes needed: **Test-engineer re-reviews**
6. **Test-engineer approves** when criteria met
7. **Solutions-architect** does final approval

---

## Questions?

- **What if developer didn't write tests?** Request tests immediately. PR cannot proceed without tests.
- **What if coverage is 79%?** Request additional tests to reach 80%. Small gap is easy to close.
- **What if tests are poorly written?** Request rework with specific examples of good tests.
- **What if I find bugs during review?** Document in review, request fixes before approval.

Contact solutions-architect for guidance.

---

**Ready to review?** Wait for developer to submit PR, then perform comprehensive test review following this checklist.
