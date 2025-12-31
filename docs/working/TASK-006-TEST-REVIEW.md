# TASK-006-TEST-REVIEW: Test-Engineer Review & Execution

## Task Metadata
- **Phase**: Week 2, Day 1-3 (Modular Foundation - Evidence Management)
- **Priority**: P1 (Core domain entity)
- **Estimated Time**: 2-3 hours
- **Dependencies**: TASK-006 (Developer submits PR with tests)
- **Assignee**: Test-Engineer
- **Reports To**: Solutions Architect

## Objective

**Run tests, review test code quality, and verify coverage** for TASK-006 (Evidence Repository Pattern):

1. **RUN all evidence tests** (unit + integration)
2. **RUN coverage analysis** and verify ≥80%
3. **RUN performance benchmarks** for evidence operations
4. **REVIEW test code quality** (edge cases, assertions, patterns)
5. **VERIFY case-evidence relationship** (foreign keys, CASCADE delete)
6. **IDENTIFY missing test scenarios**
7. **SIGN OFF** when criteria met

---

## Context

The developer implemented the Evidence Repository following the same pattern as TASK-002 (Case Repository) and TASK-003 (Session Management). Your job is to ensure:

1. Evidence CRUD operations work correctly
2. Foreign key relationship with cases table is tested
3. CASCADE delete behavior is verified
4. Primary evidence management works
5. Performance benchmarks meet targets
6. No edge cases are missed

---

## Review Criteria

### 1. Coverage Analysis ✅ MANDATORY

**Requirement:** ≥80% coverage for all evidence repository code

#### Run Coverage Report

```bash
cd /home/swhouse/product/faultmaven

# Run coverage for evidence module
pytest tests/unit/infrastructure/persistence/test_evidence_repository.py \
      tests/unit/models/test_evidence.py \
      tests/integration/test_evidence_integration.py \
      --cov=faultmaven/models/evidence \
      --cov=faultmaven/infrastructure/persistence/evidence_repository \
      --cov=faultmaven/infrastructure/persistence/database_evidence_repository \
      --cov=faultmaven/infrastructure/persistence/in_memory_evidence_repository \
      --cov-report=term-missing \
      --cov-fail-under=80
```

**Verify Coverage:**

- [ ] Overall coverage ≥ 80%
- [ ] Evidence model coverage ≥ 80%
- [ ] Database repository coverage ≥ 80%
- [ ] In-memory repository coverage ≥ 80%
- [ ] All CRUD operations tested
- [ ] All query methods tested
- [ ] Primary evidence management tested
- [ ] Error handling tested

**If coverage < 80%:** Request additional tests in PR review

---

### 2. Unit Test Quality Review - Evidence Domain Model

**File to review:** `tests/unit/models/test_evidence.py`

#### Required Test Scenarios

- [ ] `test_evidence_creation_with_required_fields` - Valid evidence object
- [ ] `test_evidence_creation_missing_required_field_fails` - Validation
- [ ] `test_evidence_post_init_validation` - Field validation
- [ ] `test_evidence_get_display_name` - Display name logic
- [ ] `test_evidence_is_image` - Image type detection
- [ ] `test_evidence_is_text` - Text type detection
- [ ] `test_evidence_type_enum_values` - All enum values valid
- [ ] `test_storage_backend_enum_values` - All storage backends valid

#### Test Quality Checklist

- [ ] **Validation:** Required field validation tested
- [ ] **Enums:** All enum values tested
- [ ] **Helper methods:** is_image(), is_text(), get_display_name() tested
- [ ] **Edge cases:** Negative file_size, empty strings, None values
- [ ] **Realistic data:** MIME types match evidence types

#### Test Anti-Patterns to Flag

- ❌ Tests that don't validate required fields
- ❌ Tests that use invalid enum values
- ❌ Tests without edge case coverage
- ❌ Missing validation for file_size < 0

---

### 3. Unit Test Quality Review - Evidence Repository

**File to review:** `tests/unit/infrastructure/persistence/test_evidence_repository.py`

#### Required Test Scenarios (CRUD)

**Create Operations:**
- [ ] `test_create_evidence_success` - Basic creation
- [ ] `test_create_evidence_duplicate_id_fails` - Duplicate prevention
- [ ] `test_create_evidence_invalid_case_id_fails` - Foreign key validation
- [ ] `test_create_evidence_sets_timestamps` - Timestamps set correctly

**Read Operations:**
- [ ] `test_get_evidence_found` - Retrieve existing evidence
- [ ] `test_get_evidence_not_found` - Returns None for missing evidence
- [ ] `test_list_evidence_by_case_empty` - Empty list for case with no evidence
- [ ] `test_list_evidence_by_case_multiple` - Multiple evidence items
- [ ] `test_list_evidence_by_case_with_type_filter` - Filter by EvidenceType
- [ ] `test_list_evidence_by_case_pagination` - Limit/offset work correctly
- [ ] `test_list_evidence_ordered_by_created_at` - Most recent first

**Update Operations:**
- [ ] `test_update_evidence_success` - Update description/metadata
- [ ] `test_update_evidence_not_found_fails` - ValueError for missing evidence
- [ ] `test_update_evidence_updates_timestamp` - updated_at changed

**Delete Operations:**
- [ ] `test_delete_evidence_success` - Returns True
- [ ] `test_delete_evidence_not_found` - Returns False

**Primary Evidence:**
- [ ] `test_get_primary_evidence_none_set` - Returns None initially
- [ ] `test_set_primary_evidence_success` - Sets is_primary=True
- [ ] `test_set_primary_evidence_unsets_previous` - Only one primary per case
- [ ] `test_set_primary_evidence_invalid_evidence_fails` - Returns False

#### Test Quality Checklist

- [ ] **Isolation:** Tests don't affect each other
- [ ] **Clarity:** Test names describe what they test
- [ ] **Assertions:** Meaningful assertions (not just "assert result")
- [ ] **Edge cases:** Missing evidence, invalid IDs, empty lists
- [ ] **Both implementations:** DatabaseRepository AND InMemoryRepository tested
- [ ] **Cleanup:** Data cleaned up after tests

#### Test Anti-Patterns to Flag

- ❌ Tests that don't clean up test data
- ❌ Tests that assume specific database state
- ❌ Tests that don't test both repository implementations
- ❌ Tests without foreign key constraint verification
- ❌ Tests that don't verify timestamps

---

### 4. Integration Test Quality Review

**File to review:** `tests/integration/test_evidence_integration.py`

#### Required Integration Scenarios

**Case-Evidence Relationship:**
- [ ] `test_evidence_cascade_delete_on_case_delete` - Evidence deleted when case deleted
- [ ] `test_evidence_preserves_case_foreign_key` - Foreign key enforced
- [ ] `test_multiple_evidence_per_case` - One case can have many evidence items

**Database Transactions:**
- [ ] `test_evidence_rollback_on_error` - Transaction rollback works
- [ ] `test_evidence_commit_persists_data` - Commit saves data

**Real Database Operations:**
- [ ] `test_evidence_with_large_metadata` - JSONB metadata works
- [ ] `test_evidence_with_unicode_filenames` - Unicode support
- [ ] `test_evidence_concurrent_primary_updates` - Race condition handling

#### Integration Test Checklist

- [ ] **Real database:** Uses actual database (not in-memory)
- [ ] **CASCADE delete:** Verified with actual foreign key
- [ ] **Transactions:** Tested rollback and commit
- [ ] **Realistic data:** Large metadata, unicode, special characters
- [ ] **Cleanup:** Database cleaned after tests

---

### 5. Performance Benchmark Review

**File to review:** `tests/benchmarks/test_evidence_operations.py`

#### Required Performance Scenarios

- [ ] `test_single_evidence_creation_latency` - Target: < 150ms
- [ ] `test_list_evidence_by_case_latency` - Target: < 100ms for 20 items
- [ ] `test_evidence_retrieval_latency` - Target: < 100ms
- [ ] `test_delete_evidence_latency` - Target: < 100ms

#### Benchmark Quality Checklist

- [ ] Uses `time.perf_counter()` for accurate timing
- [ ] Realistic test data (file sizes, MIME types)
- [ ] Asserts both success AND performance
- [ ] Prints results to console
- [ ] Follows benchmark patterns from TASK-005

#### Run Benchmarks

```bash
pytest tests/benchmarks/test_evidence_operations.py -m benchmark -v
```

**Expected Output:**
```
test_single_evidence_creation_latency PASSED
  Evidence creation latency: XXX.Xms

test_list_evidence_by_case_latency PASSED
  List evidence latency: XX.Xms (20 items)
```

**Verify:**
- [ ] All benchmarks pass
- [ ] Latencies meet targets
- [ ] Results printed clearly

---

### 6. Database Migration Review

**File to review:** `alembic/versions/20251229_1600_003_add_evidence_management.py`

#### Migration Checklist

**Schema Creation:**
- [ ] evidence table created with all columns
- [ ] evidence_id is primary key
- [ ] case_id has foreign key constraint
- [ ] Foreign key has ON DELETE CASCADE
- [ ] Indexes created for common queries
- [ ] JSONB/JSON column for metadata
- [ ] Boolean column for is_primary
- [ ] BigInteger for file_size (supports large files)

**Index Verification:**
- [ ] idx_evidence_case_id (for filtering by case)
- [ ] idx_evidence_user_id (for user queries)
- [ ] idx_evidence_organization_id (for org queries)
- [ ] idx_evidence_created_at (for ordering)
- [ ] idx_evidence_type (for filtering by type)

**Downgrade Function:**
- [ ] Drops foreign key constraint first
- [ ] Drops evidence table
- [ ] Rollback works correctly

#### Test Migration

```bash
# Run migration
alembic upgrade head

# Verify table created
# (Use database client or psql to verify schema)

# Test rollback
alembic downgrade -1

# Verify table dropped
```

**Checklist:**
- [ ] Migration runs without errors
- [ ] Table schema matches specification
- [ ] Indexes created correctly
- [ ] Foreign key constraint works
- [ ] Downgrade removes table cleanly

---

### 7. ORM Model Review

**File to review:** `faultmaven/infrastructure/persistence/models.py` (EvidenceModel class)

#### ORM Model Checklist

- [ ] Class name is `EvidenceModel`
- [ ] Inherits from `Base`
- [ ] `__tablename__ = "evidence"`
- [ ] All columns match migration
- [ ] Column types match domain model
- [ ] Relationships defined (if any)
- [ ] Repr method for debugging

#### Example Expected Structure

```python
class EvidenceModel(Base):
    """ORM model for evidence table."""

    __tablename__ = "evidence"

    evidence_id = Column(String(64), primary_key=True)
    case_id = Column(String(64), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(64), nullable=False)
    # ... other columns ...
    metadata = Column(JSON, nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
```

---

### 8. Repository Factory Review

**File to review:** `faultmaven/infrastructure/persistence/factory.py`

#### Factory Checklist

- [ ] `create_evidence_repository()` function exists
- [ ] Returns DatabaseEvidenceRepository
- [ ] Accepts database session parameter
- [ ] Type hints are correct
- [ ] Follows pattern from case_repository factory

---

### 9. Code Quality Review

#### General Code Quality

**Evidence Domain Model:**
- [ ] Type hints on all public methods
- [ ] Docstrings follow Google style
- [ ] Validation in `__post_init__`
- [ ] Helper methods (is_image, is_text) implemented
- [ ] Enums defined correctly (EvidenceType, StorageBackend)

**Database Repository:**
- [ ] Async/await used correctly
- [ ] Exception handling (IntegrityError, ValueError)
- [ ] Transaction rollback on error
- [ ] Proper ORM query patterns
- [ ] Type hints complete
- [ ] Docstrings on all public methods

**In-Memory Repository:**
- [ ] Deep copy used to prevent mutation
- [ ] Same interface as database repository
- [ ] No database dependencies
- [ ] Fast for testing

**Common Anti-Patterns to Flag:**
- ❌ Missing type hints
- ❌ No docstrings
- ❌ Hardcoded IDs or credentials
- ❌ Missing error handling
- ❌ No transaction rollback
- ❌ Mutable default arguments
- ❌ Direct database access without session

---

### 10. Missing Test Scenarios (Gap Analysis)

**Check for missing tests:**

#### Domain Model Edge Cases
- [ ] Evidence with None metadata
- [ ] Evidence with empty description
- [ ] Evidence with very large file_size (> 2GB)
- [ ] Evidence with invalid EvidenceType
- [ ] Evidence with invalid StorageBackend

#### Repository Edge Cases
- [ ] List evidence for case with 0 evidence
- [ ] List evidence with offset > total count
- [ ] List evidence with limit = 0
- [ ] Update evidence that doesn't exist
- [ ] Delete evidence twice
- [ ] Set primary evidence for wrong case

#### Integration Edge Cases
- [ ] Create evidence for non-existent case (foreign key violation)
- [ ] Delete case with 100+ evidence items (CASCADE performance)
- [ ] Concurrent updates to primary evidence
- [ ] Very large metadata (> 1MB JSON)

#### Performance Edge Cases
- [ ] List evidence for case with 100+ items
- [ ] Evidence creation with large metadata
- [ ] Bulk evidence deletion

---

## Deliverables

### 1. Coverage Report

```bash
# Generate and save coverage report
pytest tests/unit/infrastructure/persistence/test_evidence_repository.py \
      tests/unit/models/test_evidence.py \
      tests/integration/test_evidence_integration.py \
      --cov=faultmaven/models/evidence \
      --cov=faultmaven/infrastructure/persistence/evidence_repository \
      --cov=faultmaven/infrastructure/persistence/database_evidence_repository \
      --cov=faultmaven/infrastructure/persistence/in_memory_evidence_repository \
      --cov-report=html \
      --cov-report=term-missing > evidence_coverage_report.txt

# Verify threshold
pytest ... --cov-fail-under=80
```

### 2. Test Execution Report

```bash
# Run all evidence tests
pytest tests/unit/infrastructure/persistence/test_evidence_repository.py \
      tests/unit/models/test_evidence.py \
      tests/integration/test_evidence_integration.py \
      -v > evidence_test_report.txt
```

### 3. Benchmark Execution Report

```bash
# Run evidence benchmarks
pytest tests/benchmarks/test_evidence_operations.py -m benchmark -v > evidence_benchmark_report.txt
```

### 4. Test Quality Assessment

Create: `docs/working/TASK-006-TEST-REVIEW-RESULTS.md`

**Template:**

```markdown
# Test Review Results: TASK-006

## Coverage Analysis
- Overall Coverage: X%
- Evidence Model Coverage: X%
- Database Repository Coverage: X%
- In-Memory Repository Coverage: X%
- ✅/❌ Meets 80% threshold

## Unit Test Quality - Evidence Model
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Unit Test Quality - Evidence Repository
- Tests Found: X
- Required Tests Present: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Integration Test Quality
- Tests Found: X
- Required Tests Present: ✅/❌
- CASCADE Delete Verified: ✅/❌
- Foreign Key Verified: ✅/❌
- Test Quality Score: Good/Fair/Poor
- Issues Found: [list]

## Performance Benchmarks
- Benchmarks Found: X
- All Targets Met: ✅/❌
- Evidence creation: XXX ms (target: < 150ms)
- List evidence: XX ms (target: < 100ms)
- Issues Found: [list]

## Database Migration
- ✅/❌ Migration runs successfully
- ✅/❌ Table schema correct
- ✅/❌ Indexes created
- ✅/❌ Foreign key CASCADE works
- ✅/❌ Downgrade works

## Missing Test Scenarios
1. [scenario] - Priority: High/Medium/Low
2. [scenario] - Priority: High/Medium/Low

## Issues Found
1. **[Issue Title]** - Severity: Critical/Major/Minor
   - Description: [details]
   - Location: [file:line]
   - Recommendation: [fix]

## Recommendations
1. [recommendation]
2. [recommendation]

## Final Assessment
- [ ] APPROVED - Tests are production-quality
- [ ] CHANGES REQUESTED - Tests need improvements
- [ ] REJECTED - Tests inadequate, need major rework

**Justification:** [explain decision]

## Detailed Findings
[Additional notes, observations, or concerns]
```

### 5. PR Review Comment

Post to PR:

```markdown
## Test-Engineer Review: TASK-006

**Coverage:** X% (threshold: 80%) ✅/❌
**Unit Tests:** X tests - Quality: Good/Fair/Poor
**Integration Tests:** X tests - Quality: Good/Fair/Poor
**Performance Benchmarks:** ✅/❌ All targets met
**Database Migration:** ✅/❌ Verified

### Coverage Summary
- Evidence Model: X%
- Database Repository: X%
- In-Memory Repository: X%

### Critical Verification
- ✅/❌ CRUD operations work correctly
- ✅/❌ Foreign key to cases table verified
- ✅/❌ CASCADE delete tested and works
- ✅/❌ Primary evidence management works
- ✅/❌ Performance benchmarks meet targets

### Issues Found
1. [issue with specific file/line reference]

### Missing Tests
1. [missing scenario]

### Recommendations
1. [recommendation]

**Status:** ✅ APPROVED / ⚠️ CHANGES REQUESTED / ❌ NEEDS REWORK

See full review: docs/working/TASK-006-TEST-REVIEW-RESULTS.md
```

---

## Review Process

### Step 1: Checkout PR Branch

```bash
cd /home/swhouse/product/faultmaven
git fetch origin
git checkout pr-X  # Replace with actual PR branch

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-test.txt
```

### Step 2: Run Database Migration

```bash
# Run migration
alembic upgrade head

# Verify evidence table exists
# (Use database client to inspect schema)
```

### Step 3: Run All Evidence Tests (MANDATORY)

```bash
# Run all evidence tests
pytest tests/unit/infrastructure/persistence/test_evidence_repository.py \
      tests/unit/models/test_evidence.py \
      tests/integration/test_evidence_integration.py \
      -v

# Expected: All tests PASS
```

### Step 4: Run Coverage Analysis (MANDATORY)

```bash
pytest tests/unit/infrastructure/persistence/test_evidence_repository.py \
      tests/unit/models/test_evidence.py \
      tests/integration/test_evidence_integration.py \
      --cov=faultmaven/models/evidence \
      --cov=faultmaven/infrastructure/persistence/evidence_repository \
      --cov=faultmaven/infrastructure/persistence/database_evidence_repository \
      --cov=faultmaven/infrastructure/persistence/in_memory_evidence_repository \
      --cov-report=term-missing \
      --cov-report=html

# Expected: Coverage ≥ 80%
```

### Step 5: Run Performance Benchmarks

```bash
pytest tests/benchmarks/test_evidence_operations.py -m benchmark -v

# Expected: All benchmarks pass, targets met
```

### Step 6: Review Code Quality

Review implementation files:
1. `faultmaven/models/evidence.py`
2. `faultmaven/infrastructure/persistence/database_evidence_repository.py`
3. `faultmaven/infrastructure/persistence/in_memory_evidence_repository.py`
4. `faultmaven/infrastructure/persistence/models.py` (EvidenceModel)
5. `alembic/versions/20251229_1600_003_add_evidence_management.py`

Check for anti-patterns, missing validation, hardcoded values.

### Step 7: Verify CASCADE Delete (CRITICAL)

```bash
# Run CASCADE delete test specifically
pytest tests/integration/test_evidence_integration.py::test_evidence_cascade_delete_on_case_delete -v

# Expected: PASS - Evidence deleted when case deleted
```

### Step 8: Document Findings

Create `docs/working/TASK-006-TEST-REVIEW-RESULTS.md` with comprehensive review.

### Step 9: Submit Review

Post review to PR with status (APPROVED / CHANGES REQUESTED / NEEDS REWORK).

---

## Approval Criteria

### ✅ APPROVED if:

- Coverage ≥ 80%
- All required test scenarios present
- CRUD operations tested comprehensively
- CASCADE delete verified
- Foreign key constraint tested
- Primary evidence management works
- Performance benchmarks meet targets
- Test quality is good
- No major anti-patterns
- Minor issues only (can be addressed in future)

### ⚠️ CHANGES REQUESTED if:

- Coverage 70-79% (close but needs improvement)
- Some test scenarios missing (not critical paths)
- CASCADE delete works but not tested
- Test quality fair (some unclear tests, minor anti-patterns)
- Performance benchmarks close to targets (within 10%)

### ❌ NEEDS REWORK if:

- Coverage < 70%
- Critical test scenarios missing (CASCADE delete not tested)
- Tests fail or don't run
- CASCADE delete doesn't work
- Foreign key constraint not enforced
- Major test anti-patterns (hardcoded values, no cleanup)
- Poor test quality overall
- Performance benchmarks far from targets (> 20% over)

---

## Common Issues and Solutions

### Issue: Migration fails with foreign key error

**Symptom:** `alembic upgrade head` fails with foreign key constraint error

**Possible Causes:**
- cases table doesn't exist (run TASK-002 migration first)
- Previous migration not run

**Solutions:**
1. Check migration order: `alembic current`
2. Run all previous migrations: `alembic upgrade head`
3. Verify cases table exists

### Issue: Tests fail with "table does not exist"

**Symptom:** Integration tests fail with table not found error

**Possible Causes:**
- Migration not run
- Test database not initialized

**Solutions:**
1. Run migration: `alembic upgrade head`
2. Check test database connection
3. Verify test fixtures create schema

### Issue: CASCADE delete doesn't work

**Symptom:** Evidence not deleted when case deleted

**Possible Causes:**
- ON DELETE CASCADE not in migration
- Foreign key constraint not created
- Using wrong database (SQLite may not enforce)

**Solutions:**
1. Verify migration has `ondelete="CASCADE"`
2. Check foreign key constraint exists
3. Use PostgreSQL for testing (SQLite has limited FK support)

### Issue: Performance benchmarks fail

**Symptom:** Latencies exceed targets significantly

**Possible Causes:**
- Test data too large
- Indexes not created
- Slow database connection

**Solutions:**
1. Verify indexes created correctly
2. Check test data size is realistic
3. Run on consistent hardware
4. Consider adjusting targets if SQLite is slower

---

## Timeline

1. **Developer submits PR** with implementation + tests
2. **Test-engineer reviews** (2-3 hours):
   - Run all tests
   - Verify coverage
   - Run benchmarks
   - Review code quality
   - Document findings
3. If changes needed: **Developer updates tests**
4. If changes needed: **Test-engineer re-reviews**
5. **Test-engineer approves** when criteria met
6. **Solutions-architect** does final approval

---

## Questions?

- **What if CASCADE delete doesn't work in SQLite?** Test with PostgreSQL or note in review that full testing requires PostgreSQL.
- **What if coverage is 78%?** Request specific tests to reach 80%, or approve with caveat.
- **What if benchmarks are slower than targets?** Discuss with solutions-architect - targets may need adjustment for SQLite.
- **What if foreign key tests are missing?** Request integration test for foreign key constraint.

Contact solutions-architect for guidance.

---

**Ready to review?** Wait for developer to submit PR, then perform comprehensive test review including migration, tests, benchmarks, and code quality.
