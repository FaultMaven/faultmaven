# FaultMaven Testing Standards

> **Source of truth.** This is the in-repo copy so the PR checklist and docs
> can link to it on GitHub. The workspace-level copy at
> `.claude/standards/TESTING_STANDARDS.md` (parent directory, applies to all
> FaultMaven repos) must be kept in sync — update both in the same change.

This document establishes enforceable testing standards for all FaultMaven development. Testing is not optional - it is a mandatory quality gate for all code changes.

---

## Core Testing Principle

**NO CODE MERGES WITHOUT TESTS**

Every new feature, bug fix, or refactoring must include appropriate tests. Exceptions require explicit human approval with documented justification.

---

## Coverage Requirements

### Baseline Coverage
- **Current Baseline**: 71% overall coverage (as of pytest.ini configuration)
- **Minimum Threshold**: 50% (enforced by `--cov-fail-under=50` in pytest.ini)
- **Target Coverage**: 80%+ for new modules and critical business logic

### Coverage Rules
1. **Never Reduce Coverage**: New code must not decrease overall coverage percentage
2. **Critical Paths Require 90%+**: Authentication, authorization, data persistence, LLM integration
3. **Edge Cases Required**: All error conditions, validation failures, and boundary conditions must be tested
4. **No Coverage Gaming**: Writing tests that don't validate behavior just to increase coverage percentage is prohibited

---

## Test Categories

FaultMaven uses pytest markers to categorize tests. Every test must have at least one marker:

### 1. Unit Tests (`@pytest.mark.unit`)
- **Scope**: Individual functions, classes, or methods in isolation
- **Dependencies**: Mocked (no external services, databases, or APIs)
- **Speed**: Fast (< 100ms per test)
- **Coverage Target**: 80%+ for business logic

**When Required:**
- All new domain services and business logic
- All utility functions and helpers
- All data models and validators

**Example:**
```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_service_create_validates_title(mock_case_repo):
    service = CaseService(mock_case_repo)

    with pytest.raises(ValidationError):
        await service.create_case({"title": ""})  # Empty title
```

### 2. Integration Tests (`@pytest.mark.integration`)
- **Scope**: Module interactions, database operations, API endpoints
- **Dependencies**: Real databases (test DBs), Redis, ChromaDB in test mode
- **Speed**: Medium (100ms - 1s per test)
- **Coverage Target**: 70%+ for API endpoints

**When Required:**
- All new API endpoints
- All database repository methods
- All module-to-module communication
- All external service integrations

**Example:**
```python
@pytest.mark.integration
@pytest.mark.api
@pytest.mark.asyncio
async def test_create_case_endpoint_success(async_client, auth_headers, db_session):
    response = await async_client.post(
        "/api/v1/cases",
        json={"title": "Test Case", "description": "Integration test"},
        headers=auth_headers,
    )

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test Case"

    # Verify database persistence
    case = await db_session.get(Case, data["id"])
    assert case is not None
```

#### Integration Testing: File Uploads with Multipart Form Data

When testing API endpoints that accept file uploads (multipart/form-data), use **Starlette's TestClient** instead of httpx's AsyncClient.

**Why TestClient?**

- TestClient properly handles multipart form data with FastAPI's middleware stack
- AsyncClient with ASGITransport can lose multipart data when dependency_overrides is configured
- TestClient is the recommended approach for FastAPI integration tests per the framework documentation

**Example:**
```python
@pytest.mark.integration
def test_file_upload(test_case, auth_headers, mock_services_for_integration_tests):
    """Test file upload with TestClient."""
    # Strip Content-Type header - let TestClient set it for multipart
    upload_headers = {k: v for k, v in auth_headers.items()
                      if k.lower() != "content-type"}

    log_content = b"ERROR: Database timeout at 2025-01-15 14:32:11"

    response = mock_services_for_integration_tests.post(
        f"/api/v1/cases/{test_case}/data",
        files={"file": ("app.log", log_content, "text/plain")},
        data={"description": "Application logs showing timeout"},
        headers=upload_headers,
    )

    assert response.status_code == 201
    result = response.json()
    assert "evidence_id" in result
```

**Key Points:**

- Tests are synchronous (`def` not `async def`, no `@pytest.mark.asyncio`)
- Use fixture that yields a single TestClient instance (don't create new instances per test)
- Strip `Content-Type` header from auth headers to let TestClient handle multipart encoding
- Use raw bytes for file content (`b"..."`), not `BytesIO`
- The `files` parameter format is: `{"field_name": (filename, content, mime_type)}`
- Additional form fields go in the `data` parameter

**Fixture Pattern:**
```python
@pytest.fixture
def mock_services_for_integration_tests(app, mock_user):
    """Configure mocks and return TestClient for file upload tests."""
    # Setup all dependency overrides BEFORE creating TestClient
    app.dependency_overrides[require_authentication] = lambda: mock_user
    app.dependency_overrides[get_case_service] = get_mock_case_service
    # ... more overrides ...

    # Create TestClient ONCE with all overrides configured
    test_client = TestClient(app)
    yield test_client

    # Cleanup
    test_client.close()
    app.dependency_overrides.clear()
```

### 3. Performance Tests (`@pytest.mark.performance`)
- **Scope**: Response time, throughput, scalability
- **Dependencies**: Production-like data volumes
- **Speed**: Slow (1s+ per test)
- **Coverage Target**: Critical paths only

**When Required:**
- Changes to search/query operations
- Bulk operations (batch processing)
- LLM integration changes
- Vector search modifications

**Example:**
```python
@pytest.mark.performance
@pytest.mark.slow
@pytest.mark.asyncio
async def test_bulk_case_search_performance(async_client, auth_headers, load_1000_cases):
    import time

    start = time.time()
    response = await async_client.get(
        "/api/v1/cases/search?q=test&limit=100",
        headers=auth_headers,
    )
    duration = time.time() - start

    assert response.status_code == 200
    assert duration < 2.0  # Must complete in under 2 seconds
```

### 4. Security Tests (`@pytest.mark.security`)
- **Scope**: Authentication, authorization, input validation, data leakage
- **Dependencies**: Varies
- **Speed**: Fast to medium
- **Coverage Target**: 100% for auth/authz paths

**When Required:**
- All authentication/authorization changes
- All input validation changes
- JWT handling modifications
- Any user data access changes

**Example:**
```python
@pytest.mark.security
@pytest.mark.asyncio
async def test_case_access_denied_for_other_user(async_client, auth_headers, other_user_case):
    response = await async_client.get(
        f"/api/v1/cases/{other_user_case.id}",
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert "access denied" in response.json()["detail"].lower()
```

---

## Test-Driven Development Workflow

### For New Features

1. **Before Writing Code**:
   - Write failing tests that define expected behavior
   - Review test scenarios with team/architect if complex
   - Ensure tests cover happy path + edge cases + error conditions

2. **During Development**:
   - Run tests frequently (`pytest -k test_name`)
   - Fix tests as you refine understanding
   - Add more tests as you discover edge cases

3. **Before Committing**:
   - Run full test suite: `pytest`
   - Verify coverage didn't decrease: `pytest --cov=faultmaven --cov-report=term-missing`
   - Fix any failing tests (never commit broken tests)

4. **In Pull Request**:
   - Include test coverage report in PR description
   - Highlight new test files and coverage delta
   - Document any coverage exemptions with justification

### For Bug Fixes

1. **Write Failing Test First**:
   - Create a test that reproduces the bug
   - Verify the test fails with current code
   - Document the bug scenario in test docstring

2. **Fix the Bug**:
   - Implement the fix
   - Verify the new test passes
   - Ensure existing tests still pass

3. **Verify Fix**:
   - Run full test suite
   - Check that the regression is prevented
   - Add additional edge case tests if needed

### For Refactoring

1. **Verify Existing Tests**:
   - Run all tests before refactoring: `pytest`
   - Document current coverage: `pytest --cov`
   - Identify any missing test coverage

2. **Add Missing Tests**:
   - If coverage < 70%, add tests before refactoring
   - Focus on the code you're about to change

3. **Refactor with Green Tests**:
   - Make incremental changes
   - Run tests after each change
   - Never refactor with failing tests

4. **Post-Refactor Validation**:
   - All original tests must still pass
   - Coverage must not decrease
   - Add new tests if refactor reveals untested paths

---

## Running Tests

### Local Development

```bash
# Run all tests
pytest

# Run specific module tests
pytest tests/case/

# Run specific test markers
pytest -m unit                    # Only unit tests (fast)
pytest -m integration             # Only integration tests
pytest -m "not slow"              # Exclude slow tests

# Run with coverage
pytest --cov=faultmaven --cov-report=html

# Run specific test file
pytest tests/case/test_case_service.py

# Run specific test function
pytest tests/case/test_case_service.py::test_create_case_success

# Run with verbose output
pytest -v

# Run with debug output
pytest -vv --tb=long
```

### Pre-Commit Checks

Before every commit, run:
```bash
# Fast feedback loop (unit tests only)
pytest -m unit

# Full validation before pushing
pytest --cov=faultmaven --cov-report=term-missing
```

### CI/CD Pipeline

All tests run automatically on:
- Every pull request
- Every merge to main
- Nightly regression runs

**CI Requirements:**
- All tests must pass (zero failures)
- Coverage must not decrease
- No new test warnings or errors

---

## Mocking Guidelines

### What to Mock

1. **External Services**: LLM APIs, third-party APIs
2. **Expensive Operations**: Large file I/O, network calls
3. **Non-deterministic Behavior**: Random values, timestamps
4. **External Infrastructure**: Email services, SMS gateways

### What NOT to Mock

1. **Database Layer in Integration Tests**: Use test database
2. **Simple Functions**: If it's fast and deterministic, use the real thing
3. **Internal Module Logic**: Test actual behavior, not mocks

### Mocking Best Practices

```python
# Good: Mock external LLM API
@pytest.fixture
def mock_openai_client():
    with patch("openai.AsyncOpenAI") as mock:
        mock.return_value.chat.completions.create = AsyncMock(
            return_value=OpenAIResponse(
                choices=[Choice(message=Message(content="Mocked response"))]
            )
        )
        yield mock

# Good: Mock ChromaDB for unit tests
@pytest.fixture
def mock_chromadb_collection():
    collection = MagicMock()
    collection.query.return_value = {
        "ids": [["doc_1"]],
        "documents": [["Sample document"]],
        "distances": [[0.85]],
    }
    return collection

# Bad: Over-mocking internal logic
# Don't mock your own domain services in integration tests
```

---

## Test Data Management

### Test Fixtures

Use pytest fixtures for reusable test data:

```python
# conftest.py
@pytest.fixture
async def sample_user(db_session):
    """Create a test user."""
    user = User(
        id="user_123",
        email="test@example.com",
        hashed_password="hashed_password_here"
    )
    db_session.add(user)
    await db_session.commit()
    return user

@pytest.fixture
async def auth_headers(sample_user):
    """Generate JWT auth headers."""
    token = create_access_token({"sub": sample_user.id})
    return {"Authorization": f"Bearer {token}"}
```

### Factory Pattern

For complex objects, use factory_boy:

```python
import factory
from factory import Factory, Faker

class CaseFactory(Factory):
    class Meta:
        model = Case

    id = Faker("uuid4")
    title = Faker("sentence")
    description = Faker("paragraph")
    user_id = "user_123"
    status = "open"
    created_at = Faker("date_time")

# Usage in tests
case = CaseFactory.create(title="Specific Title")
```

---

## PR Testing Checklist

Before submitting a pull request, verify:

- [ ] All new code has corresponding tests
- [ ] All tests pass locally: `pytest`
- [ ] Coverage has not decreased: `pytest --cov`
- [ ] New endpoints have integration tests
- [ ] New business logic has unit tests
- [ ] Error conditions are tested
- [ ] Security-critical changes have security tests
- [ ] Performance-critical changes have performance tests
- [ ] Test names clearly describe what they test
- [ ] Mocks are used appropriately (not over-mocked)
- [ ] Test data is minimal and focused
- [ ] No commented-out tests (delete or fix)
- [ ] No skipped tests without documented reason

---

## Exemptions and Exceptions

### When Tests Are Optional

1. **Trivial Changes**: Typo fixes, comment updates, formatting
2. **Exploratory Prototypes**: Explicitly marked as proof-of-concept
3. **Emergency Hotfixes**: With explicit architect approval and follow-up task

### Requesting an Exemption

If you believe tests are not needed:

1. Document reason in PR description
2. Flag it for human review
3. Create follow-up task if deferring tests
4. Get explicit approval before merging

**Note**: "Didn't have time" is NOT a valid exemption reason.

---

## Test Maintenance

### Keeping Tests Green

- Fix failing tests immediately (within same PR)
- Never commit commented-out tests
- Never use `@pytest.mark.skip` without a linked issue
- Delete obsolete tests (don't just disable)

### Refactoring Tests

When tests become hard to maintain:
- Extract common setup into fixtures
- Use parametrized tests for similar scenarios
- Split large test files into focused modules
- Remove duplicate test logic

### Test Performance

If tests become slow:
- Profile with `pytest --durations=10`
- Move slow tests to separate suite
- Optimize test data setup
- Use markers to allow fast feedback loops

---

## Integration with Development Workflow

### Git Workflow

```bash
# 1. Create feature branch
git checkout -b feature/add-bulk-delete

# 2. Write tests first (TDD)
# ... create test files ...

# 3. Run tests (should fail)
pytest tests/case/test_bulk_delete.py

# 4. Implement feature
# ... write code ...

# 5. Verify tests pass
pytest tests/case/test_bulk_delete.py

# 6. Run full suite
pytest

# 7. Check coverage
pytest --cov=faultmaven --cov-report=term-missing

# 8. Commit with tests
git add tests/case/test_bulk_delete.py
git add faultmaven/modules/case/domain/bulk_operations.py
git commit -m "feat: add bulk delete operation with comprehensive tests"

# 9. Push and create PR
git push -u origin feature/add-bulk-delete
```

### PR Review Focus

Reviewers must verify:
1. Tests exist for all new code
2. Tests validate actual behavior (not just mocks)
3. Edge cases are covered
4. Error conditions are tested
5. Test names are descriptive
6. Coverage delta is positive or neutral

---

## Measuring Success

### Coverage Metrics

Track these metrics in every PR:
- **Overall Coverage**: Current percentage vs baseline (71%)
- **Module Coverage**: Per-module breakdown
- **Uncovered Lines**: Specific lines needing tests
- **Coverage Delta**: Change from previous commit

### Quality Metrics

- **Test Pass Rate**: Should be 100%
- **Test Execution Time**: Track trends, optimize if increasing
- **Flaky Tests**: Zero tolerance (fix or delete)
- **Test Code Ratio**: Aim for 1:1 or higher (test LOC:source LOC)

---

## Resources

### Documentation
- [Test Engineer Agent](../../.claude/agents/test-engineer.md) - Specialized agent for writing tests
- **Agent Core Principles** (workspace-level `.claude/standards/agent-principles.md`) - Testing is part of "Verify, Don't Guess"
- [pytest Documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)

### Test Examples
- `/home/swhouse/product/faultmaven/tests/` - 118+ test files
- Look for tests marked with your domain: `@pytest.mark.case`, `@pytest.mark.knowledge`, etc.

### Getting Help
- **For test strategy**: Use `/test-engineer` agent
- **For test failures**: Debug locally first, then consult test-engineer
- **For coverage questions**: Check pytest.ini configuration

---

## Enforcement

These standards are enforced through:

1. **Automated Checks**: CI/CD pipeline fails if tests fail or coverage decreases
2. **PR Reviews**: Human reviewer verifies test requirements
3. **Git Hooks**: Pre-commit hooks run fast unit tests
4. **Agent Compliance**: All FaultMaven agents must follow these standards

**Non-compliance results in PR rejection and required rework.**

---

## Summary

Testing is not a nice-to-have - it's a fundamental quality requirement. Following these standards ensures:
- Fewer production bugs
- Faster development cycles (tests catch issues early)
- Confident refactoring (tests prevent regressions)
- Better documentation (tests show how code works)
- Higher code quality (testable code is better designed)

**When in doubt, write more tests, not fewer.**
