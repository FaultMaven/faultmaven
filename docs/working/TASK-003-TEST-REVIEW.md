# TASK-003-TEST-REVIEW: Test-Engineer Review & Execution

## Task Metadata
- **Phase**: Week 1, Day 4-5 (Foundation - Test Review)
- **Priority**: P0 (Blocks TASK-003 approval)
- **Estimated Time**: 1-2 hours
- **Dependencies**: TASK-003 (Developer submits PR with tests)
- **Assignee**: Test-Engineer
- **Reports To**: Solutions Architect

## Objective

**Run tests, review test code quality, and verify coverage** for TASK-003 (Session Management Integration):

1. **RUN all tests** and verify they pass
2. **RUN coverage analysis** and verify ≥80%
3. **REVIEW test code quality** (edge cases, assertions, proper patterns)
4. **IDENTIFY missing test scenarios**
5. **SIGN OFF** when criteria met

## Context

The developer implemented session management integration with the case repository and wrote unit + integration tests. Your job is to ensure the tests themselves are production-quality before solutions-architect final approval.

## Review Criteria

### 1. Coverage Analysis ✅ MANDATORY

**Requirement:** ≥80% coverage for all new code

**Check:**
```bash
# Run coverage report
pytest --cov=faultmaven/infrastructure/persistence/session_repository \
       --cov=faultmaven/domain/models/session \
       --cov-report=term-missing

# Verify coverage threshold
pytest --cov=faultmaven/infrastructure/persistence --cov-fail-under=80
```

**Review:**
- [ ] Overall coverage ≥ 80%
- [ ] All session CRUD methods have tests
- [ ] Session-case linking tested
- [ ] Foreign key constraint behavior tested
- [ ] Session cleanup logic tested
- [ ] No untested critical code paths

**If coverage < 80%:** Request additional tests in PR review

---

### 2. Unit Test Quality Review

**Files to review:**
- `tests/unit/infrastructure/persistence/test_database_session_repository.py`
- `tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py`

#### Required Test Scenarios - Session Repository
- [ ] `test_create_session` - Happy path session creation
- [ ] `test_get_session` - Retrieve existing session
- [ ] `test_get_session_not_found` - Return None for non-existent session
- [ ] `test_update_last_accessed` - Update session timestamp
- [ ] `test_delete_session` - Delete session
- [ ] `test_cleanup_expired_sessions` - Clean up expired sessions only
- [ ] `test_session_expiry_check` - Session expiry logic

#### Required Test Scenarios - Case-Session Integration
- [ ] `test_create_case_with_session` - Case linked to session
- [ ] `test_create_case_without_session` - Case without session (NULL)
- [ ] `test_get_cases_by_session` - Retrieve all cases for session
- [ ] `test_get_cases_by_session_empty` - Empty list when no cases
- [ ] `test_get_orphaned_cases` - Retrieve cases with no session
- [ ] `test_session_delete_orphans_cases` - ON DELETE SET NULL behavior

#### Test Quality Checklist
- [ ] **Isolation:** Each test is independent (no shared state)
- [ ] **Clarity:** Test names describe what they test
- [ ] **Assertions:** Meaningful assertions (not just "assert result")
- [ ] **Edge cases:** Boundary conditions tested (expired sessions, NULL values)
- [ ] **Mocking:** Proper use of mocks/fixtures (database session mocked in unit tests)
- [ ] **Async/await:** All async functions properly awaited
- [ ] **Cleanup:** Proper teardown (no test data leaks)
- [ ] **Timestamps:** UTC timestamp handling tested

#### Test Anti-Patterns to Flag
- ❌ Tests that depend on execution order
- ❌ Tests with hardcoded timestamps that could fail randomly
- ❌ Tests that don't verify foreign key constraint behavior
- ❌ Tests without assertions for NULL session_id cases
- ❌ Tests that use real database in unit tests (should use mocks)
- ❌ Multiple unrelated assertions in one test (should split)
- ❌ Tests that don't verify session cleanup preserves cases

---

### 3. Integration Test Quality Review

**File to review:** `tests/integration/test_session_case_integration.py`

#### Required Integration Scenarios
- [ ] `test_session_case_lifecycle` - Create session → Create case → Retrieve → Cleanup
- [ ] `test_session_cleanup_preserves_cases` - Deleting session doesn't delete cases
- [ ] `test_multiple_cases_per_session` - Multiple cases per session
- [ ] `test_session_expiry_workflow` - Expired session cleanup with active cases
- [ ] `test_repository_factory_session` - Factory returns correct implementation

#### Integration Test Checklist
- [ ] **Real database:** Uses test SQLite database (not mocks)
- [ ] **Isolation:** Each test uses fresh database or transaction rollback
- [ ] **Cleanup:** Database cleaned after each test
- [ ] **Fixtures:** Proper use of pytest fixtures for DB setup
- [ ] **Realistic data:** Tests use realistic session/case data
- [ ] **Foreign keys:** Tests verify ON DELETE SET NULL works
- [ ] **Transactions:** Tests verify transaction boundaries
- [ ] **Migration:** Tests verify migration applies/rolls back cleanly

---

### 4. Test Fixtures and Configuration Review

**File to review:** `tests/conftest.py`

#### Required Fixtures
- [ ] `db_session` - Provides test database session
- [ ] `session_repository` - Provides session repository instance
- [ ] `case_repository` - Provides case repository instance (from TASK-002)
- [ ] `sample_session` - Factory for creating test sessions
- [ ] `sample_case` - Factory for creating test cases (from TASK-002)
- [ ] Database cleanup (autouse fixture or explicit cleanup)

#### Configuration Checklist
- [ ] Test database URL configured (not production DB!)
- [ ] Fixtures properly scoped (function/module/session)
- [ ] Async fixtures use `@pytest_asyncio.fixture`
- [ ] No hardcoded credentials or secrets
- [ ] Session expiry times reasonable for tests (not 24 hours)

---

### 5. Migration Testing Review

**Check Alembic migration**

- [ ] Migration file created: `002_add_session_management.py`
- [ ] `upgrade()` creates sessions table
- [ ] `upgrade()` adds session_id column to cases
- [ ] `upgrade()` creates foreign key constraint with ON DELETE SET NULL
- [ ] `upgrade()` creates indexes (session_id, user_id)
- [ ] `downgrade()` removes everything cleanly
- [ ] Migration tested: `alembic upgrade head` succeeds
- [ ] Rollback tested: `alembic downgrade -1` succeeds
- [ ] Migration idempotent (can run multiple times safely)

**Test migration:**
```bash
# Apply migration
alembic upgrade head

# Verify tables created
alembic current

# Test rollback
alembic downgrade -1

# Re-apply
alembic upgrade head
```

---

### 6. Missing Test Scenarios (Gap Analysis)

Look for these common missing tests:

#### Error Handling
- [ ] Session creation with duplicate ID
- [ ] Session retrieval with invalid ID format
- [ ] Expired session access handling
- [ ] NULL user_id handling
- [ ] Invalid metadata format

#### Edge Cases
- [ ] Session with no expiry (expires_at = NULL)
- [ ] Session cleanup when no expired sessions
- [ ] Case creation with non-existent session_id
- [ ] Multiple sessions for same user
- [ ] Very long session metadata (JSON size limits)

#### Foreign Key Behavior
- [ ] Deleting session sets case.session_id to NULL
- [ ] Creating case with invalid session_id fails
- [ ] Orphaned cases queryable after session delete
- [ ] Cascading deletes don't occur (verify SET NULL)

#### Timestamp Edge Cases
- [ ] Session created with future timestamp
- [ ] Session created with past timestamp
- [ ] Timezone handling (UTC enforcement)
- [ ] last_accessed update on retrieval

---

## Deliverables

### 1. Coverage Report
```bash
# Generate and save coverage report
pytest --cov=faultmaven/infrastructure/persistence \
       --cov=faultmaven/domain/models/session \
       --cov-report=html \
       --cov-report=term-missing > coverage_report.txt

# Check threshold
pytest --cov=faultmaven/infrastructure/persistence --cov-fail-under=80
```

### 2. Test Quality Assessment

Create: `docs/working/TASK-003-TEST-REVIEW-RESULTS.md`

**Template:**
```markdown
# Test Review Results: TASK-003

## Coverage Analysis
- Overall Coverage: X%
- Session Repository Coverage: X%
- Case-Session Integration Coverage: X%
- ✅/❌ Meets 80% threshold

## Unit Test Quality
- Session Repository Tests: X tests
- Case-Session Tests: X tests
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Integration Test Quality
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Migration Testing
- ✅/❌ Migration applies successfully
- ✅/❌ Migration rolls back successfully
- ✅/❌ Foreign key constraint works (ON DELETE SET NULL)
- ✅/❌ Indexes created

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
## Test-Engineer Review: TASK-003

**Coverage:** X% (threshold: 80%) ✅/❌
**Unit Tests:** X tests - Quality: Good/Fair/Poor
**Integration Tests:** X tests - Quality: Good/Fair/Poor
**Migration:** ✅/❌ Tested (up and down)

### Issues Found
1. [issue with specific line reference]
2. [issue with specific line reference]

### Missing Tests
1. [missing scenario]
2. [missing scenario]

### Recommendations
1. [recommendation]

**Status:** ✅ APPROVED / ⚠️ CHANGES REQUESTED / ❌ NEEDS REWORK

See full review: docs/working/TASK-003-TEST-REVIEW-RESULTS.md
```

---

## Review Process

### Step 1: Checkout PR Branch
```bash
cd /home/swhouse/product/faultmaven
git fetch origin
git checkout pr-X  # or appropriate PR branch

# Install dependencies
pip install -e .
pip install -r requirements-test.txt
```

### Step 2: RUN MIGRATION TESTS (MANDATORY)
```bash
# Test migration up
alembic upgrade head

# Verify current revision
alembic current

# Verify tables
sqlite3 test_sqlite.db ".schema sessions"
sqlite3 test_sqlite.db ".schema cases"

# Test migration down
alembic downgrade -1

# Verify rollback
alembic current

# Re-apply migration
alembic upgrade head
```

**Expected:** All migration operations succeed without errors

### Step 3: RUN TESTS (MANDATORY)
```bash
# Run all tests and verify they pass
pytest -v

# Run unit tests only
pytest tests/unit/infrastructure/persistence/test_database_session_repository.py -v
pytest tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py -v

# Run integration tests only
pytest tests/integration/test_session_case_integration.py -v

# Check test count
pytest tests/unit/infrastructure/persistence/ --collect-only | grep "test session starts"
pytest tests/integration/test_session_case_integration.py --collect-only | grep "test session starts"
```

**Expected:** All tests PASS (no failures, no errors)

### Step 4: RUN COVERAGE ANALYSIS (MANDATORY)
```bash
# Generate coverage report
pytest tests/unit/infrastructure/persistence/test_database_session_repository.py \
       tests/unit/infrastructure/persistence/test_database_case_repository_sessions.py \
       tests/integration/test_session_case_integration.py \
       --cov=faultmaven/infrastructure/persistence/session_repository \
       --cov=faultmaven/infrastructure/persistence/database_case_repository \
       --cov=faultmaven/domain/models/session \
       --cov-report=term-missing \
       --cov-report=html

# Verify threshold
pytest tests/unit/infrastructure/persistence/ \
       tests/integration/test_session_case_integration.py \
       --cov=faultmaven/infrastructure/persistence \
       --cov=faultmaven/domain/models/session \
       --cov-fail-under=80
```

**Expected:** Coverage ≥80%

### Step 5: Open Coverage Report
```bash
# View detailed coverage in browser
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

**Verify:** No critical code paths untested, especially:
- Session creation and retrieval
- Session cleanup (expired sessions)
- Foreign key constraint (ON DELETE SET NULL)
- Orphaned case queries

### Step 6: Review Test Code Quality
- Read test files (see quality criteria above)
- Check for anti-patterns
- Note missing scenarios
- Verify foreign key behavior tested

### Step 7: Document Findings
- Create TASK-003-TEST-REVIEW-RESULTS.md
- Document all findings
- Provide specific line references
- Give actionable recommendations

### Step 8: Submit Review
Post to PR with test results:

```markdown
## Test-Engineer Review: TASK-003

### Test Execution Results
- ✅/❌ All tests pass: X passed, Y failed
- ✅/❌ Coverage: X% (threshold: 80%)
- ✅/❌ Migration tested (up and down)

### Test Quality Assessment
- Unit tests: X tests - Quality: Good/Fair/Poor
- Integration tests: X tests - Quality: Good/Fair/Poor

### Migration Quality
- ✅/❌ Sessions table created
- ✅/❌ Foreign key constraint works
- ✅/❌ ON DELETE SET NULL verified

### Issues Found
1. [issue]

### Missing Tests
1. [missing scenario]

### Status
✅ APPROVED / ⚠️ CHANGES REQUESTED

See: docs/working/TASK-003-TEST-REVIEW-RESULTS.md
```

---

## Approval Criteria

### ✅ APPROVED if:
- Coverage ≥ 80%
- All required test scenarios present
- Migration tested (up and down)
- Foreign key constraint behavior verified
- Test quality is good (clear, isolated, meaningful assertions)
- No major anti-patterns
- All tests pass locally and CI/CD
- Minor issues only (can be addressed in future)

### ⚠️ CHANGES REQUESTED if:
- Coverage 70-79% (close but needs improvement)
- Some test scenarios missing (not critical paths)
- Test quality fair (some unclear tests, minor anti-patterns)
- All tests pass but quality needs improvement
- Migration not fully tested

### ❌ NEEDS REWORK if:
- Coverage < 70%
- Critical test scenarios missing (foreign key behavior, session cleanup)
- Major test anti-patterns (shared state, race conditions)
- Tests failing
- Migration fails or doesn't roll back cleanly
- Poor test quality overall

---

## Test Quality Examples

### ✅ GOOD TEST
```python
@pytest.mark.unit
async def test_create_session_success():
    """Test creating a new session returns session with correct attributes"""
    # Arrange
    repo = DatabaseSessionRepository(mock_session)
    session_data = Session(
        session_id="sess_123",
        user_id="user_456",
        created_at=datetime.utcnow(),
        last_accessed=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=24),
        metadata={"client": "web"}
    )

    # Act
    result = await repo.create_session(session_data)

    # Assert
    assert result.session_id == "sess_123"
    assert result.user_id == "user_456"
    assert result.metadata["client"] == "web"
    assert result.expires_at is not None
```

### ✅ GOOD INTEGRATION TEST
```python
@pytest.mark.integration
async def test_session_cleanup_preserves_cases():
    """Test deleting session sets case.session_id to NULL, preserves case"""
    # Arrange
    session_repo = DatabaseSessionRepository(db_session)
    case_repo = DatabaseCaseRepository(db_session)

    # Create session
    session = await session_repo.create_session(
        Session(session_id="sess_1", user_id="user_1", ...)
    )

    # Create case linked to session
    case = await case_repo.create_case(
        Case(title="Test", user_id="user_1", ...),
        session_id="sess_1"
    )

    # Act
    await session_repo.delete_session("sess_1")

    # Assert
    retrieved_case = await case_repo.get_case(case.case_id)
    assert retrieved_case is not None  # Case still exists
    assert retrieved_case.session_id is None  # But session link is NULL
```

### ❌ BAD TEST
```python
async def test_sessions():
    """Test sessions"""
    repo = get_repo()
    session = repo.create_session({"user_id": "user1"})
    assert session  # What does this verify?
```

**Issues:**
- No `@pytest.mark.unit` marker
- Name too vague ("sessions")
- Docstring unhelpful
- No Arrange/Act/Assert structure
- Missing async decorator
- Weak assertion (just checks truthy)
- Doesn't test specific behavior

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
- **What if migration doesn't roll back cleanly?** Request fix. Migration must be reversible.
- **What if foreign key constraint not tested?** Request integration test verifying ON DELETE SET NULL.
- **What if I find bugs during review?** Document in review, request fixes before approval.

Contact solutions-architect for guidance.

---

**Ready to review?** Wait for developer to submit PR, then perform comprehensive test review following this checklist.
