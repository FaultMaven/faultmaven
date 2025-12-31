# TASK-005-TEST-REVIEW: Test-Engineer Review & Execution

## Task Metadata
- **Phase**: Week 1, Day 8-10 (Foundation - Performance Baseline)
- **Priority**: P1 (Establishes regression detection)
- **Estimated Time**: 2-3 hours
- **Dependencies**: TASK-005 (Developer submits PR with benchmarks)
- **Assignee**: Test-Engineer
- **Reports To**: Solutions Architect

## Objective

**Verify performance benchmarks execute correctly and establish baseline measurements** for TASK-005 (Performance Baseline Suite):

1. **RUN all benchmarks** and verify they complete without errors
2. **VERIFY performance targets** are met (latency, throughput, memory)
3. **EXECUTE load tests** and verify reports are generated
4. **VALIDATE CI integration** works correctly
5. **CAPTURE baseline results** for future regression detection
6. **REVIEW benchmark code quality** (realistic scenarios, accurate measurements)
7. **SIGN OFF** when criteria met

## Context

The developer implemented a comprehensive performance benchmark suite to establish baselines before adding new features. Your job is to ensure:

1. Benchmarks run successfully and measure correctly
2. Performance targets are realistic and met
3. Load testing infrastructure works
4. CI integration catches regressions
5. Documentation is clear and actionable

---

## Review Criteria

### 1. Benchmark Execution ✅ MANDATORY

**Requirement:** All benchmarks run without errors

#### Run All Benchmarks

```bash
cd /home/swhouse/product/faultmaven

# Install benchmark dependencies
poetry install --with benchmark

# Run all benchmarks
pytest tests/benchmarks/ -m benchmark -v

# Verify pytest marker is registered
pytest --markers | grep benchmark
```

**Expected Output:**
```
tests/benchmarks/test_case_operations.py::TestCaseCreationPerformance::test_single_case_creation_latency PASSED
✓ Case creation latency: XXX.Xms

tests/benchmarks/test_session_operations.py::TestSessionOperationPerformance::test_session_creation_latency PASSED
✓ Session creation latency: XX.Xms

... (all benchmarks pass)
```

#### Checklist

- [ ] All benchmark tests execute successfully (no crashes)
- [ ] pytest `-m benchmark` marker works correctly
- [ ] Latency measurements printed to console
- [ ] No database errors or connection issues
- [ ] Fixtures create clean test data
- [ ] Tests clean up after themselves (no data leakage)

**If benchmarks crash:** Request fix before proceeding

---

### 2. Performance Target Verification ✅ MANDATORY

**Requirement:** All performance targets are met

#### Latency Targets

Run benchmarks and capture output:

```bash
pytest tests/benchmarks/ -m benchmark -v 2>&1 | tee benchmark_output.txt
```

**Verify Targets:**

| Benchmark | Target | Status |
|-----------|--------|--------|
| Case creation | < 200ms | ✅/❌ |
| Case retrieval | < 100ms | ✅/❌ |
| Case update | < 150ms | ✅/❌ |
| List cases (50) | < 150ms | ✅/❌ |
| Session creation | < 50ms | ✅/❌ |
| Session retrieval | < 30ms | ✅/❌ |
| Session cleanup | > 1000/sec | ✅/❌ |

#### Memory Targets

```bash
pytest tests/benchmarks/test_memory_usage.py -m benchmark -v
```

**Verify:**
- [ ] Baseline memory < 100MB RSS
- [ ] Memory under load < 512MB RSS
- [ ] No memory leaks (memory returns to baseline after cleanup)

#### Checklist

- [ ] All latency targets met
- [ ] All throughput targets met
- [ ] All memory targets met
- [ ] Targets are realistic (not artificially low/high)
- [ ] Measurements are accurate (using `time.perf_counter()`)

**If targets not met:**
- Is this expected? (SQLite is slower than PostgreSQL)
- Are targets too aggressive? (discuss with solutions-architect)
- Is there a performance bug? (investigate)

---

### 3. Load Testing Verification ✅ MANDATORY

**Requirement:** Load testing infrastructure works

#### Start FaultMaven Locally

```bash
# Terminal 1: Start FaultMaven
cd /home/swhouse/product/faultmaven
./scripts/start.sh
```

#### Run Load Tests

```bash
# Terminal 2: Run load test
./scripts/run_load_tests.sh local
```

**Expected:**
- Locust starts successfully
- Requests are sent to local API
- HTML report generated
- CSV results generated
- No excessive error rate (< 1%)

#### Verify Reports

```bash
# Find latest results
ls -lt benchmark_results/

# Open HTML report (if running locally with browser)
open benchmark_results/YYYYMMDD_HHMMSS/report.html
```

**Check Report:**
- [ ] Total requests > 100
- [ ] Failure rate < 1%
- [ ] p95 latency < 500ms
- [ ] Charts render correctly
- [ ] CSV files contain data

#### Checklist

- [ ] `run_load_tests.sh` script is executable
- [ ] Locust starts without errors
- [ ] Load test completes successfully
- [ ] HTML report generated
- [ ] CSV results generated
- [ ] Results directory created with timestamp
- [ ] No critical errors during load test

**If load test fails:** Check API is running, check locustfile.py syntax

---

### 4. CI Integration Verification

**Requirement:** GitHub Actions workflow runs benchmarks

#### Verify Workflow File

**File to review:** `.github/workflows/benchmarks.yml`

```bash
cat .github/workflows/benchmarks.yml
```

**Checklist:**
- [ ] Workflow triggers on PR to main
- [ ] Workflow triggers on push to main
- [ ] Workflow scheduled weekly
- [ ] Python 3.11 used
- [ ] Poetry installs benchmark dependencies
- [ ] pytest runs with `-m benchmark`
- [ ] Results uploaded as artifact
- [ ] Artifact retention set (90 days)

#### Simulate CI Run (Local)

```bash
# Simulate CI environment
poetry install --with benchmark
poetry run pytest tests/benchmarks/ -m benchmark --tb=short -v
```

**Expected:** All tests pass in CI-like environment

#### Checklist

- [ ] Workflow syntax is valid (no YAML errors)
- [ ] All steps are correct
- [ ] Benchmark dependencies installed
- [ ] Results can be uploaded (check artifact paths)
- [ ] PR comment script is present (even if TODO)

**Note:** Full CI verification happens when PR is created

---

### 5. Baseline Results Capture ✅ MANDATORY

**Requirement:** Baseline measurements recorded for future comparison

#### Capture Baselines

```bash
# Run benchmarks and save output
pytest tests/benchmarks/ -m benchmark -v 2>&1 | tee baseline_results.txt

# Extract key metrics
grep "✓" baseline_results.txt
```

**Document Baselines:**

Create a baseline summary:

```bash
# Example extraction
Case creation latency: 145.3ms
Case retrieval latency: 78.2ms
Session creation latency: 38.5ms
Session retrieval latency: 22.1ms
Memory baseline: 87.4 MB RSS
Memory under load: 456.2 MB RSS
```

#### Verify Baseline File

**File to review:** `.github/benchmark_baselines/baseline_v1.json`

```bash
cat .github/benchmark_baselines/baseline_v1.json
```

**Checklist:**
- [ ] Baseline file exists
- [ ] All critical metrics included
- [ ] Version and date documented
- [ ] Environment documented (Python version, OS, database)
- [ ] Notes explain baseline context

#### Update Baseline (If Needed)

If baseline file has placeholder values, update with actual measurements:

```json
{
  "version": "1.0.0",
  "created_at": "2025-12-29T12:00:00Z",
  "environment": {
    "python_version": "3.11",
    "os": "ubuntu-latest",
    "database": "sqlite"
  },
  "baselines": {
    "case_creation_latency_ms": 145,
    "case_retrieval_latency_ms": 78,
    "session_creation_latency_ms": 38,
    "session_retrieval_latency_ms": 22,
    "list_cases_latency_ms": 120,
    "memory_baseline_mb": 87,
    "memory_under_load_mb": 456
  },
  "notes": "Initial baseline before feature additions"
}
```

**Checklist:**
- [ ] Baseline values match actual measurements
- [ ] Baseline is realistic (not artificially low)
- [ ] Baseline documented in PR

---

### 6. Benchmark Code Quality Review

**Files to review:**

1. `tests/benchmarks/conftest.py` - Fixtures
2. `tests/benchmarks/test_case_operations.py` - Case benchmarks
3. `tests/benchmarks/test_session_operations.py` - Session benchmarks
4. `tests/benchmarks/test_memory_usage.py` - Memory benchmarks
5. `tests/load/locustfile.py` - Load testing script

#### Fixture Quality (`conftest.py`)

**Checklist:**
- [ ] Fixtures are properly scoped (`session`, `function`)
- [ ] Database engine uses in-memory SQLite (fast, isolated)
- [ ] SQL logging disabled (`echo=False`) for clean output
- [ ] Schema created before tests run
- [ ] Engine properly disposed after tests
- [ ] Async fixtures use proper type hints

#### Benchmark Quality (Case Operations)

**File:** `tests/benchmarks/test_case_operations.py`

**Checklist:**
- [ ] Uses `time.perf_counter()` for accurate timing
- [ ] Latency measured correctly (start before operation, end after)
- [ ] Database commit included in measurement (realistic)
- [ ] Assertions check both success AND performance
- [ ] Error messages include actual vs target latency
- [ ] Results printed to console for visibility
- [ ] Test data is realistic (not trivial)
- [ ] Batch operations test throughput correctly

#### Example Good Benchmark

```python
@pytest.mark.asyncio
async def test_single_case_creation_latency(
    self,
    case_repository,
    benchmark_session
):
    """Measure latency of creating a single case.

    Target: p95 < 200ms
    """
    case = Case(
        case_id="benchmark-case-001",
        title="Benchmark Test Case",
        description="Performance benchmark for case creation",
        status=CaseStatus.OPEN,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    start = time.perf_counter()
    result = await case_repository.create_case(case)
    await benchmark_session.commit()  # Include commit in measurement
    latency = time.perf_counter() - start

    assert result is not None  # Verify success
    assert latency < 0.200, (  # Verify performance
        f"Case creation latency {latency*1000:.1f}ms exceeds 200ms target"
    )
    print(f"\n✓ Case creation latency: {latency*1000:.1f}ms")  # Visibility
```

**Why this is excellent:**
- ✅ Clear docstring with target
- ✅ Realistic test data
- ✅ Accurate timing (perf_counter)
- ✅ Includes commit (realistic workflow)
- ✅ Asserts both success and performance
- ✅ Helpful error message
- ✅ Prints result for visibility

#### Benchmark Anti-Patterns to Flag

- ❌ Using `time.time()` instead of `time.perf_counter()` (less accurate)
- ❌ Not including database commit in measurement (unrealistic)
- ❌ Trivial test data (empty strings, minimal data)
- ❌ No assertions (just measurements without validation)
- ❌ Hardcoded IDs that conflict between tests
- ❌ No cleanup (data leaks between tests)
- ❌ Targets are unrealistic (1ms for database operation)

#### Load Test Quality (`locustfile.py`)

**Checklist:**
- [ ] Realistic user behavior (wait times, task distribution)
- [ ] Multiple task types (create, read, update)
- [ ] Proper weight distribution (`@task(3)`, `@task(5)`)
- [ ] Random data generation (no hardcoded values)
- [ ] Error handling (check response codes)
- [ ] Stress test user included (aggressive testing)
- [ ] Docstring explains usage
- [ ] No hardcoded credentials

---

### 7. Documentation Quality Review

**File to review:** `docs/development/performance-testing.md`

**Checklist:**
- [ ] Quick start section (how to run benchmarks)
- [ ] Performance targets table (clear, specific)
- [ ] Load testing instructions (local and staging)
- [ ] Interpreting results section (how to read output)
- [ ] CI integration explained
- [ ] Regression detection process
- [ ] Profiling tools recommended
- [ ] Baseline management process
- [ ] Examples are accurate and working

#### Test Documentation Commands

```bash
# Verify all commands in documentation work
cd /home/swhouse/product/faultmaven

# From docs/development/performance-testing.md
poetry install --with benchmark
pytest tests/benchmarks/ -m benchmark -v
pytest tests/benchmarks/test_case_operations.py -m benchmark -v
./scripts/run_load_tests.sh local  # (requires running FaultMaven)
```

**Expected:** All documented commands work without errors

---

### 8. Missing Scenarios (Gap Analysis)

**Check for missing benchmarks:**

#### Database Operations
- [ ] Bulk operations (create 100 cases at once)
- [ ] Complex queries (filters, sorting, pagination)
- [ ] Transaction rollback performance
- [ ] Connection pool exhaustion

#### Concurrency
- [ ] Multiple concurrent creates
- [ ] Read-write contention
- [ ] Deadlock handling

#### Edge Cases
- [ ] Large case descriptions (10KB+)
- [ ] Large result sets (1000+ cases)
- [ ] Empty database (no data)
- [ ] Database under memory pressure

#### Future Features
- [ ] Knowledge search (placeholder present?)
- [ ] Evidence upload (future)
- [ ] Agent execution (future)

**Note:** Not all missing scenarios are blockers. Document them for future work.

---

## Deliverables

### 1. Benchmark Execution Report

Run benchmarks and save output:

```bash
pytest tests/benchmarks/ -m benchmark -v 2>&1 | tee benchmark_execution_report.txt
```

**Include in review:**
- All test results (PASSED/FAILED)
- Actual latency measurements
- Memory usage measurements
- Any warnings or errors

### 2. Load Test Results

Run load test and save results:

```bash
./scripts/run_load_tests.sh local

# Save results location
ls -l benchmark_results/$(ls -t benchmark_results/ | head -1)/
```

**Include in review:**
- Total requests
- Requests per second
- p50/p95/p99 latencies
- Failure rate
- Screenshot or summary of HTML report

### 3. Baseline Measurements

Document actual baseline measurements:

```
## Baseline Measurements (2025-12-29)

**Environment:**
- Python: 3.11
- Database: SQLite (in-memory)
- OS: Ubuntu/Linux

**Latency Baselines:**
- Case creation: XXX.X ms
- Case retrieval: XX.X ms
- Case update: XXX.X ms
- List cases: XXX.X ms
- Session creation: XX.X ms
- Session retrieval: XX.X ms

**Memory Baselines:**
- Baseline: XX.X MB RSS
- Under load: XXX.X MB RSS

**Throughput Baselines:**
- Case creation: XX cases/sec
- Session cleanup: XXXX sessions/sec
```

### 4. Test Quality Assessment

Create: `docs/working/TASK-005-TEST-REVIEW-RESULTS.md`

**Template:**

```markdown
# Test Review Results: TASK-005

## Benchmark Execution

- ✅/❌ All benchmarks run successfully
- Tests executed: X
- Tests passed: X
- Tests failed: X
- Execution time: X seconds

## Performance Target Verification

| Benchmark | Target | Actual | Status |
|-----------|--------|--------|--------|
| Case creation | < 200ms | XXX ms | ✅/❌ |
| Case retrieval | < 100ms | XX ms | ✅/❌ |
| Session creation | < 50ms | XX ms | ✅/❌ |
| Session retrieval | < 30ms | XX ms | ✅/❌ |
| Memory baseline | < 100MB | XX MB | ✅/❌ |
| Memory under load | < 512MB | XXX MB | ✅/❌ |

**Overall:** ✅ All targets met / ⚠️ Some targets missed / ❌ Multiple targets missed

## Load Testing

- ✅/❌ Load test script runs successfully
- ✅/❌ HTML report generated
- ✅/❌ CSV results generated
- ✅/❌ Failure rate < 1%

**Summary:**
- Total requests: XXX
- Requests/sec: XX
- p95 latency: XXX ms
- Failure rate: X%

## CI Integration

- ✅/❌ Workflow file is valid
- ✅/❌ Workflow configuration correct
- ✅/❌ Dependencies installed correctly
- ✅/❌ Artifacts configured

## Baseline Capture

- ✅/❌ Baseline file exists
- ✅/❌ All metrics documented
- ✅/❌ Environment documented
- ✅/❌ Values are realistic

## Benchmark Code Quality

**Fixtures (conftest.py):** Good/Fair/Poor
- Issues found: [list or "None"]

**Case Operations:** Good/Fair/Poor
- Issues found: [list or "None"]

**Session Operations:** Good/Fair/Poor
- Issues found: [list or "None"]

**Memory Usage:** Good/Fair/Poor
- Issues found: [list or "None"]

**Load Test (locustfile.py):** Good/Fair/Poor
- Issues found: [list or "None"]

## Documentation Quality

**performance-testing.md:** Good/Fair/Poor
- Issues found: [list or "None"]
- ✅/❌ All commands work
- ✅/❌ Examples are accurate
- ✅/❌ Clear and actionable

## Missing Scenarios

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

- [ ] APPROVED - Benchmarks are production-quality
- [ ] CHANGES REQUESTED - Benchmarks need improvements
- [ ] REJECTED - Benchmarks inadequate, need major rework

**Justification:** [explain decision]

## Detailed Findings

[Additional notes, observations, or concerns]
```

### 5. PR Review Comment

Post to PR:

```markdown
## Test-Engineer Review: TASK-005

**Benchmark Execution:** ✅/❌ X tests passed
**Performance Targets:** ✅/❌ All targets met
**Load Testing:** ✅/❌ Reports generated successfully
**CI Integration:** ✅/❌ Workflow configured correctly
**Baseline Capture:** ✅/❌ Baselines documented

### Performance Summary

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Case creation | < 200ms | XXX ms | ✅/❌ |
| Memory usage | < 512MB | XXX MB | ✅/❌ |

### Load Test Results

- Total requests: XXX
- Throughput: XX req/sec
- p95 latency: XXX ms
- Failure rate: X%

### Issues Found

1. [issue with specific file/line reference]

### Missing Benchmarks

1. [missing scenario]

### Recommendations

1. [recommendation]

**Status:** ✅ APPROVED / ⚠️ CHANGES REQUESTED / ❌ NEEDS REWORK

See full review: docs/working/TASK-005-TEST-REVIEW-RESULTS.md
```

---

## Review Process

### Step 1: Checkout PR Branch

```bash
cd /home/swhouse/product/faultmaven
git fetch origin
git checkout pr-X  # Replace with actual PR branch

# Install dependencies
poetry install --with benchmark
```

### Step 2: Run All Benchmarks (MANDATORY)

```bash
# Run benchmarks and capture output
pytest tests/benchmarks/ -m benchmark -v 2>&1 | tee benchmark_output.txt

# Check for errors
echo $?  # Should be 0 (success)
```

**Expected:** All tests PASS

### Step 3: Verify Performance Targets

Review `benchmark_output.txt` and verify:

- [ ] All latency targets met
- [ ] All throughput targets met
- [ ] All memory targets met
- [ ] No test failures
- [ ] No warnings or errors

### Step 4: Run Load Tests

```bash
# Terminal 1: Start FaultMaven
./scripts/start.sh

# Terminal 2: Run load test
./scripts/run_load_tests.sh local

# Verify results
ls -l benchmark_results/$(ls -t benchmark_results/ | head -1)/
```

**Expected:** HTML and CSV reports generated

### Step 5: Review Code Quality

Review benchmark files:

1. `tests/benchmarks/conftest.py` - Fixtures
2. `tests/benchmarks/test_case_operations.py` - Case benchmarks
3. `tests/benchmarks/test_session_operations.py` - Session benchmarks
4. `tests/benchmarks/test_memory_usage.py` - Memory benchmarks
5. `tests/load/locustfile.py` - Load testing

Check for anti-patterns, realistic scenarios, accurate measurements.

### Step 6: Verify CI Configuration

```bash
# Check workflow file
cat .github/workflows/benchmarks.yml

# Verify workflow syntax (requires GitHub CLI)
gh workflow view benchmarks
```

### Step 7: Capture Baselines

Extract actual measurements from `benchmark_output.txt`:

```bash
grep "✓" benchmark_output.txt
```

Document in review results.

### Step 8: Verify Documentation

```bash
# Check documentation exists
cat docs/development/performance-testing.md

# Test commands from documentation
pytest tests/benchmarks/ -m benchmark -v
```

### Step 9: Document Findings

Create `docs/working/TASK-005-TEST-REVIEW-RESULTS.md` with comprehensive review.

### Step 10: Submit Review

Post review to PR with status (APPROVED / CHANGES REQUESTED / NEEDS REWORK).

---

## Approval Criteria

### ✅ APPROVED if:

- All benchmarks run successfully (no crashes)
- All performance targets met OR justified exceptions
- Load testing infrastructure works
- CI workflow configured correctly
- Baselines documented
- Benchmark code quality is good
- Documentation is clear and accurate
- Minor issues only (can be addressed in future)

### ⚠️ CHANGES REQUESTED if:

- Some benchmarks fail (non-critical)
- Performance targets slightly missed (within 10%)
- Load test has minor issues (doesn't block)
- CI workflow needs minor fixes
- Code quality is fair (some improvements needed)
- Documentation has minor gaps

### ❌ NEEDS REWORK if:

- Benchmarks crash or don't run
- Critical performance targets missed (> 10%)
- Load testing doesn't work at all
- CI workflow is broken
- Baselines not documented
- Code quality is poor (major anti-patterns)
- Documentation is missing or incorrect

---

## Common Issues and Solutions

### Issue: Benchmarks are too slow

**Symptom:** All benchmarks exceed targets by 2-3x

**Possible Causes:**
- Running on slow machine
- Database not using in-memory SQLite
- Debug logging enabled
- Other processes consuming resources

**Solutions:**
1. Verify SQLite in-memory is used: `sqlite+aiosqlite:///:memory:`
2. Disable SQL logging: `echo=False` in engine creation
3. Close other applications
4. Run on CI for consistent environment

### Issue: Benchmarks are non-deterministic

**Symptom:** Same benchmark shows widely different results (50ms vs 200ms)

**Possible Causes:**
- Background processes
- Garbage collection during measurement
- Cold start vs warm start

**Solutions:**
1. Run multiple times and take median
2. Add warmup phase before measurement
3. Use larger sample sizes (batch operations)

### Issue: Load test fails with connection errors

**Symptom:** Locust shows high failure rate, connection refused

**Possible Causes:**
- FaultMaven not running
- Wrong port
- API not fully started

**Solutions:**
1. Verify FaultMaven is running: `curl http://localhost:8000/health`
2. Wait for full startup before load test
3. Check logs for errors

### Issue: Memory benchmarks fail

**Symptom:** Memory usage exceeds targets significantly

**Possible Causes:**
- Memory leaks
- Test data not cleaned up
- Other tests running concurrently

**Solutions:**
1. Run memory tests in isolation
2. Add explicit cleanup
3. Use `gc.collect()` before measurement
4. Check for circular references

---

## Timeline

1. **Developer submits PR** with benchmark implementation
2. **Test-engineer reviews** (2-3 hours):
   - Run all benchmarks
   - Verify targets
   - Run load tests
   - Review code quality
   - Document findings
3. If changes needed: **Developer updates benchmarks**
4. If changes needed: **Test-engineer re-reviews**
5. **Test-engineer approves** when criteria met
6. **Solutions-architect** does final approval

---

## Questions?

- **What if targets are not met?** Check if targets are realistic for SQLite. May need adjustment.
- **How to handle non-deterministic results?** Run multiple times, take median/average.
- **What if load test fails?** Verify FaultMaven is running and accessible.
- **What if CI workflow doesn't work?** Test locally first, verify syntax with `gh workflow view`.

Contact solutions-architect for guidance.

---

**Ready to review?** Wait for developer to submit PR, then perform comprehensive benchmark verification including execution, targets, load testing, and code quality.
