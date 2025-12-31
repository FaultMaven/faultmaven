# TASK-004-TEST-REVIEW: Test-Engineer Review & Execution

## Task Metadata
- **Phase**: Week 1, Day 4-7 (Foundation - Test Review)
- **Priority**: P1 (Blocks TASK-004 approval)
- **Estimated Time**: 1-2 hours
- **Dependencies**: TASK-004 (Developer submits PR with tests)
- **Assignee**: Test-Engineer
- **Reports To**: Solutions Architect

## Objective

**Run tests, review test code quality, and verify coverage** for TASK-004 (Minimal Shim Pattern Foundation):

1. **RUN all tests** with dependencies installed
2. **RUN all tests** with dependencies NOT installed
3. **RUN coverage analysis** and verify ≥80%
4. **REVIEW test code quality** (edge cases, assertions, proper patterns)
5. **IDENTIFY missing test scenarios**
6. **SIGN OFF** when criteria met

## Context

The developer implemented shim layers for Opik (observability) and Presidio (PII redaction) with graceful degradation. Your job is to ensure:
1. Tests verify shims work with dependencies installed
2. Tests verify shims work with dependencies NOT installed (no-op fallback)
3. Environment variable toggles tested
4. No crashes when dependencies missing

## Review Criteria

### 1. Coverage Analysis ✅ MANDATORY

**Requirement:** ≥80% coverage for all shim code

**Check:**
```bash
# Run coverage report
pytest --cov=faultmaven/infrastructure/shims --cov-report=term-missing

# Verify coverage threshold
pytest --cov=faultmaven/infrastructure/shims --cov-fail-under=80
```

**Review:**
- [ ] Overall coverage ≥ 80%
- [ ] Feature detection (try/except ImportError) tested
- [ ] No-op fallbacks tested
- [ ] Environment variable toggles tested
- [ ] Error handling tested

**If coverage < 80%:** Request additional tests in PR review

---

### 2. Unit Test Quality Review - Observability Shim

**File to review:** `tests/unit/infrastructure/shims/test_observability.py`

#### Required Test Scenarios
- [ ] `test_track_decorator_with_opik_installed` - Real Opik usage
- [ ] `test_track_decorator_without_opik` - No-op fallback
- [ ] `test_track_decorator_opik_disabled` - ENABLE_TRACING=false
- [ ] `test_track_decorator_opik_enabled` - ENABLE_TRACING=true
- [ ] `test_track_decorator_preserves_function_behavior` - Function still works
- [ ] `test_get_tracing_status` - Status reporting

#### Test Quality Checklist
- [ ] **Isolation:** Tests don't affect each other
- [ ] **Clarity:** Test names describe what they test
- [ ] **Assertions:** Meaningful assertions (not just "assert result")
- [ ] **Edge cases:** Both with and without dependencies tested
- [ ] **Mocking:** Proper mocking of environment variables
- [ ] **Async/await:** Async decorators properly awaited
- [ ] **Cleanup:** Environment variables restored after tests

#### Test Anti-Patterns to Flag
- ❌ Tests that don't mock environment variables (test isolation)
- ❌ Tests that assume Opik is installed (should test both cases)
- ❌ Tests that don't verify no-op behavior
- ❌ Tests without environment variable cleanup

---

### 3. Unit Test Quality Review - Security Shim

**File to review:** `tests/unit/infrastructure/shims/test_security.py`

#### Required Test Scenarios
- [ ] `test_pii_redactor_with_presidio_installed` - Real Presidio usage
- [ ] `test_pii_redactor_without_presidio` - Pass-through fallback
- [ ] `test_pii_redactor_disabled` - ENABLE_PII_REDACTION=false
- [ ] `test_pii_redactor_enabled` - ENABLE_PII_REDACTION=true
- [ ] `test_pii_redactor_redacts_email` - Email PII redacted
- [ ] `test_pii_redactor_redacts_phone` - Phone PII redacted
- [ ] `test_pii_redactor_error_handling` - Fail-open behavior
- [ ] `test_pii_redactor_get_status` - Status reporting

#### Test Quality Checklist
- [ ] **Isolation:** Each test independent
- [ ] **Clarity:** Clear test names
- [ ] **Assertions:** Specific assertions (verify redaction worked)
- [ ] **Edge cases:** Empty text, None, long text
- [ ] **Mocking:** Environment variables mocked
- [ ] **Error cases:** Presidio initialization failure tested
- [ ] **Cleanup:** Environment variables restored

#### Test Anti-Patterns to Flag
- ❌ Tests that assume Presidio is installed
- ❌ Tests that don't verify pass-through when disabled
- ❌ Tests that don't verify fail-open on error
- ❌ Hardcoded PII in tests (use fixtures)

---

### 4. Integration Test Quality Review

**File to review:** `tests/integration/test_shims_integration.py`

#### Required Integration Scenarios
- [ ] `test_case_service_with_shims_enabled` - Full workflow with shims
- [ ] `test_case_service_with_shims_disabled` - Full workflow without shims
- [ ] `test_environment_variable_toggling` - Dynamic configuration

#### Integration Test Checklist
- [ ] **Real usage:** Tests actual service integration
- [ ] **Both modes:** Tests with and without dependencies
- [ ] **Realistic data:** Uses realistic test data
- [ ] **End-to-end:** Tests full workflow (not just shims)
- [ ] **Cleanup:** Environment restored after tests

---

### 5. Dependency Testing (CRITICAL)

**Special Requirement:** Verify shims work with dependencies NOT installed

#### Manual Test (Required)
```bash
# Create clean virtual environment
python -m venv test_venv_no_deps
source test_venv_no_deps/bin/activate

# Install FaultMaven without Opik/Presidio
pip install -e . --no-deps
pip install <only core dependencies>

# Run tests - should pass with no-op shims
pytest tests/unit/infrastructure/shims/ -v

# Verify application starts
python -m faultmaven --help

# Cleanup
deactivate
rm -rf test_venv_no_deps
```

**Expected:** All tests pass, application starts without errors

#### Checklist
- [ ] Tests pass with dependencies installed
- [ ] Tests pass with dependencies NOT installed
- [ ] Application starts without dependencies
- [ ] No ImportError exceptions raised
- [ ] Logging indicates shim status clearly

---

### 6. Test Fixtures and Configuration Review

**File to review:** `tests/conftest.py` (if shim fixtures added)

#### Required Fixtures (if present)
- [ ] Environment variable mocking fixtures
- [ ] Mock Opik/Presidio objects (if needed)
- [ ] Cleanup fixtures (autouse for env vars)

#### Configuration Checklist
- [ ] Fixtures properly scoped
- [ ] Environment variables restored after tests
- [ ] No hardcoded credentials or secrets
- [ ] Mock objects match real API

---

### 7. Missing Test Scenarios (Gap Analysis)

Look for these common missing tests:

#### Error Handling
- [ ] Opik initialization failure
- [ ] Presidio initialization failure
- [ ] Invalid environment variable values
- [ ] Malformed input to redactor

#### Edge Cases
- [ ] Empty text to redactor
- [ ] None input to redactor
- [ ] Very long text (performance)
- [ ] Special characters in text
- [ ] Multiple PII types in one text

#### Configuration
- [ ] ENABLE_TRACING with different case (True, TRUE, true)
- [ ] Invalid environment variable values
- [ ] Environment variables changed at runtime

---

## Deliverables

### 1. Coverage Report
```bash
# Generate and save coverage report
pytest --cov=faultmaven/infrastructure/shims \
       --cov-report=html \
       --cov-report=term-missing > coverage_report.txt

# Check threshold
pytest --cov=faultmaven/infrastructure/shims --cov-fail-under=80
```

### 2. Test Quality Assessment

Create: `docs/working/TASK-004-TEST-REVIEW-RESULTS.md`

**Template:**
```markdown
# Test Review Results: TASK-004

## Coverage Analysis
- Overall Coverage: X%
- Observability Shim Coverage: X%
- Security Shim Coverage: X%
- ✅/❌ Meets 80% threshold

## Unit Test Quality - Observability
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Unit Test Quality - Security
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Integration Test Quality
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Dependency Testing (CRITICAL)
- ✅/❌ Tests pass with dependencies installed
- ✅/❌ Tests pass with dependencies NOT installed
- ✅/❌ Application starts without dependencies
- ✅/❌ No ImportError exceptions

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
## Test-Engineer Review: TASK-004

**Coverage:** X% (threshold: 80%) ✅/❌
**Unit Tests:** X tests - Quality: Good/Fair/Poor
**Integration Tests:** X tests - Quality: Good/Fair/Poor
**Dependency Testing:** ✅/❌ Verified both modes

### Issues Found
1. [issue with specific line reference]
2. [issue with specific line reference]

### Missing Tests
1. [missing scenario]
2. [missing scenario]

### Critical Verification
- ✅/❌ Shims work with dependencies installed
- ✅/❌ Shims work with dependencies NOT installed
- ✅/❌ Environment variables toggle features
- ✅/❌ No crashes when dependencies missing

### Recommendations
1. [recommendation]

**Status:** ✅ APPROVED / ⚠️ CHANGES REQUESTED / ❌ NEEDS REWORK

See full review: docs/working/TASK-004-TEST-REVIEW-RESULTS.md
```

---

## Review Process

### Step 1: Checkout PR Branch
```bash
cd /home/swhouse/product/faultmaven
git fetch origin
git checkout pr-X  # or appropriate PR branch

# Install dependencies (WITH Opik and Presidio)
pip install -e .
pip install -r requirements-test.txt
pip install opik presidio-analyzer presidio-anonymizer  # Optional deps
```

### Step 2: RUN TESTS (With Dependencies) - MANDATORY
```bash
# Set environment variables to enable features
export ENABLE_TRACING=true
export ENABLE_PII_REDACTION=true

# Run all shim tests
pytest tests/unit/infrastructure/shims/ -v
pytest tests/integration/test_shims_integration.py -v

# Check test count
pytest tests/unit/infrastructure/shims/ --collect-only
```

**Expected:** All tests PASS with features enabled

### Step 3: RUN TESTS (Without Dependencies) - MANDATORY
```bash
# Create clean virtual environment
python -m venv test_no_deps
source test_no_deps/bin/activate

# Install FaultMaven without optional dependencies
pip install -e .
pip install pytest pytest-asyncio  # Test dependencies only

# Run tests - should use no-op shims
pytest tests/unit/infrastructure/shims/ -v

# Verify import works
python -c "from faultmaven.infrastructure.shims import track, PIIRedactor; print('OK')"

# Cleanup
deactivate
rm -rf test_no_deps
```

**Expected:** All tests PASS with no-op fallbacks

### Step 4: RUN COVERAGE ANALYSIS (MANDATORY)
```bash
# Generate coverage report
pytest tests/unit/infrastructure/shims/ \
       tests/integration/test_shims_integration.py \
       --cov=faultmaven/infrastructure/shims \
       --cov-report=term-missing \
       --cov-report=html

# Verify threshold
pytest tests/unit/infrastructure/shims/ \
       tests/integration/test_shims_integration.py \
       --cov=faultmaven/infrastructure/shims \
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
- Feature detection (try/except ImportError)
- No-op fallback functions
- Environment variable checks

### Step 6: Review Test Code Quality
- Read test files (see quality criteria above)
- Check for anti-patterns
- Note missing scenarios
- Verify both modes tested (with/without deps)

### Step 7: Document Findings
- Create TASK-004-TEST-REVIEW-RESULTS.md
- Document all findings
- Provide specific line references
- Give actionable recommendations

### Step 8: Submit Review
Post to PR with test results:

```markdown
## Test-Engineer Review: TASK-004

### Test Execution Results
- ✅/❌ All tests pass (with dependencies): X passed, Y failed
- ✅/❌ All tests pass (without dependencies): X passed, Y failed
- ✅/❌ Coverage: X% (threshold: 80%)

### Dependency Testing (CRITICAL)
- ✅/❌ Shims work with dependencies installed
- ✅/❌ Shims work with dependencies NOT installed
- ✅/❌ Application starts without optional dependencies

### Test Quality Assessment
- Observability tests: X tests - Quality: Good/Fair/Poor
- Security tests: X tests - Quality: Good/Fair/Poor
- Integration tests: X tests - Quality: Good/Fair/Poor

### Issues Found
1. [issue]

### Missing Tests
1. [missing scenario]

### Status
✅ APPROVED / ⚠️ CHANGES REQUESTED

See: docs/working/TASK-004-TEST-REVIEW-RESULTS.md
```

---

## Approval Criteria

### ✅ APPROVED if:
- Coverage ≥ 80%
- All required test scenarios present
- Tests pass WITH dependencies installed
- Tests pass WITHOUT dependencies installed (CRITICAL)
- Application starts without optional dependencies
- Test quality is good (clear, isolated, meaningful assertions)
- No major anti-patterns
- Environment variable toggles tested
- Minor issues only (can be addressed in future)

### ⚠️ CHANGES REQUESTED if:
- Coverage 70-79% (close but needs improvement)
- Some test scenarios missing (not critical paths)
- Tests pass with dependencies but not without
- Test quality fair (some unclear tests, minor anti-patterns)

### ❌ NEEDS REWORK if:
- Coverage < 70%
- Critical test scenarios missing (no-op fallbacks not tested)
- Tests fail with or without dependencies
- Application crashes when dependencies missing
- Major test anti-patterns (hardcoded env vars, no cleanup)
- Poor test quality overall

---

## Test Quality Examples

### ✅ GOOD TEST - Dependency Detection
```python
@pytest.mark.unit
def test_track_decorator_without_opik(monkeypatch):
    """Test track decorator when Opik is NOT installed (no-op)."""
    # Arrange - Simulate Opik not installed
    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.observability.OPIK_AVAILABLE",
        False
    )
    monkeypatch.setenv("ENABLE_TRACING", "true")

    from faultmaven.infrastructure.shims import track

    @track("test_operation")
    async def test_func():
        return "result"

    # Act
    result = await test_func()

    # Assert - Function works, no tracing
    assert result == "result"
    # Verify no-op decorator was used (function not wrapped by Opik)
```

**Why this is excellent:**
- ✅ Tests critical scenario (dependency not installed)
- ✅ Mocks OPIK_AVAILABLE to simulate missing dependency
- ✅ Verifies function still works (no crash)
- ✅ Clear documentation of what's being tested

### ✅ GOOD TEST - Environment Variable Toggle
```python
@pytest.mark.unit
def test_pii_redactor_disabled(monkeypatch):
    """Test PIIRedactor when ENABLE_PII_REDACTION=false."""
    # Arrange
    monkeypatch.setenv("ENABLE_PII_REDACTION", "false")

    redactor = PIIRedactor()
    test_text = "My email is user@example.com"

    # Act
    result = redactor.redact(test_text)

    # Assert - Text unchanged (pass-through)
    assert result == test_text
    assert redactor.active is False
```

**Why this is excellent:**
- ✅ Tests environment variable control
- ✅ Verifies pass-through behavior when disabled
- ✅ Checks internal state (active flag)

### ❌ BAD TEST
```python
def test_shims():
    """Test shims work"""
    from faultmaven.infrastructure.shims import track

    @track("test")
    def func():
        return True

    assert func()
```

**Issues:**
- ❌ No `@pytest.mark.unit` marker
- ❌ Doesn't test both modes (with/without dependencies)
- ❌ Doesn't mock environment variables
- ❌ Weak assertion (just checks truthy)
- ❌ No cleanup (env vars not restored)
- ❌ Doesn't verify no-op vs real behavior

---

## Timeline

1. **Developer submits PR** with implementation + tests
2. **Test-engineer reviews** (1-2 hours)
3. **Test-engineer tests WITHOUT dependencies** (CRITICAL)
4. **Test-engineer posts findings** to PR
5. If changes needed: **Developer updates tests**
6. If changes needed: **Test-engineer re-reviews**
7. **Test-engineer approves** when criteria met
8. **Solutions-architect** does final approval

---

## Questions?

- **What if I don't have Opik/Presidio installed?** Good! Test the no-op mode first.
- **How do I test without dependencies?** Use a clean virtual environment (see Step 3 above).
- **What if tests fail without dependencies?** Request fix - this is the critical path.
- **What if environment variable tests are missing?** Request tests for ENABLE_* toggles.

Contact solutions-architect for guidance.

---

**Ready to review?** Wait for developer to submit PR, then perform comprehensive test review including dependency testing.
