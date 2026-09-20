# Performance Testing Guide

This guide explains how to run performance benchmarks and interpret results
for the FaultMaven platform.

## Quick Start

### Run All Benchmarks

```bash
# Install dependencies (if not already installed)
pip install -r requirements-test.txt

# Run all benchmarks
pytest tests/benchmarks/ -m benchmark -v
```

### Run Specific Benchmark Suites

```bash
# Case operation benchmarks only
pytest tests/benchmarks/test_case_operations.py -m benchmark -v

# Session operation benchmarks only
pytest tests/benchmarks/test_session_operations.py -m benchmark -v

# Memory usage benchmarks
pytest tests/benchmarks/test_memory_usage.py -m benchmark -v
```

## Performance Targets

### Case Operations

| Operation | Target | Measurement |
|-----------|--------|-------------|
| Case creation | < 200ms | p95 latency |
| Case retrieval | < 100ms | p95 latency |
| Case update | < 150ms | p95 latency |
| List cases (50) | < 150ms | p95 latency |
| Search cases | < 200ms | p95 latency |
| Batch creation | > 50/sec | throughput |

### Session Operations

| Operation | Target | Measurement |
|-----------|--------|-------------|
| Session creation | < 50ms | p95 latency |
| Session retrieval | < 30ms | p95 latency |
| Update last_accessed | < 30ms | p95 latency |
| Session deletion | < 30ms | p95 latency |
| Session cleanup | > 500/sec | throughput |

### Knowledge Search (Future)

| Operation | Target | Measurement |
|-----------|--------|-------------|
| Vector search | < 300ms | p95 latency |
| Embedding generation | < 100ms | p95 latency |
| RAG pipeline | < 500ms | end-to-end |

### Memory Usage

| Metric | Target | Measurement |
|--------|--------|-------------|
| Baseline | < 100MB | RSS at startup |
| Under load | < 512MB | RSS with 10 cases |

## Load Testing

### Local Load Test

```bash
# Start FaultMaven locally first
python -m uvicorn faultmaven.main:app --host 0.0.0.0 --port 8000

# Run load test (separate terminal)
./scripts/run_load_tests.sh local
```

### Staging Load Test

```bash
./scripts/run_load_tests.sh staging
```

### Custom Load Test with Locust UI

```bash
# Run with UI (opens browser at http://localhost:8089)
locust -f tests/load/locustfile.py --host=http://localhost:8090
```

### View Results

Load test results are saved to `benchmark_results/YYYYMMDD_HHMMSS/`:
- `report.html` - Interactive HTML report
- `results_stats.csv` - Statistics in CSV format
- `output.log` - Console output

## Interpreting Results

### Latency Benchmarks

Example output:
```
  Case creation latency: 145.3ms
  Case retrieval latency: 78.2ms
```

- **Passes target**: Meets performance requirement
- **Fails target**: Exceeds limit (regression detected)

### Memory Benchmarks

Example output:
```
  Baseline memory usage: 87.4 MB RSS
  Memory usage under load: 456.2 MB RSS (+368.8 MB)
```

**Interpretation:**
- **Baseline**: Memory at test startup
- **Delta**: Additional memory for workload
- **Target**: Total < 512MB

### Load Test Metrics

Key metrics from Locust:
- **Requests/sec**: Throughput (higher is better)
- **p50/p95/p99**: Latency percentiles (lower is better)
- **Failure rate**: Percentage of failed requests (0% is ideal)

## Budgets are calibrated, not absolute (#908)

A latency threshold written in milliseconds and asserted on a GitHub-hosted
runner does not measure the code. #908 quantified that from the runs' own
`benchmark_results.json` artifacts: comparing a **failing** run to a
**passing** run of the *same commit*, the median per-test ratio across all
30 tests was **1.28, uniform across every one of them**; against a run of
*different* code it was 0.98. No code path was slower — the whole pytest
process scaled with machine throughput, and the thinnest-margin test was
whichever happened to be closest to its number that week.

So the thresholds stayed (they encode product targets) and the instrument
changed. `tests/benchmarks/calibration.py` measures a fixed, cheap,
CPU-bound workload **in the same pytest process**, and every budget is
`target * calibration_scale()`:

```
budget      = target        x scale
floor       = target        / scale        (throughput: it is 1/latency)
scale       = max(1.0, measured / CALIBRATION_REFERENCE_SECONDS)
```

Three properties worth knowing before you read a result:

* **The scale never drops below 1.0.** A machine at or above the reference
  speed is held to exactly the number written in the test, so the scale can
  never tighten a budget — on CI or on your laptop. (One unrelated part of
  #908 is a hair stricter: the nine budgets in
  `test_investigation_session_service_operations` moved from `p95 <= target`
  to the shared helper's `observed < budget`, so a p95 landing exactly on
  the target now fails. Float timings make that unreachable in practice.)
* **A uniform slowdown cancels; a single-path regression does not.** That
  is the whole point, and it is asserted both ways in
  `tests/unit/ci/test_benchmark_calibration.py`.
* **Memory assertions are not scaled.** Megabytes do not move with machine
  throughput, and correcting them would be nonsense.

### How much headroom the budgets have today

Worth knowing before you read a red run, and worth re-measuring before
anyone argues about a threshold. Joining every budget in the suite with the
number the same test reported in two green `main` runs (35499510079 and
35496672222), **no budget is within 50% of its target on either**: the
highest utilisation is 27.7% and 20.0% (`test_batch_case_creation_throughput`
in both), and the median is 2.6% and 2.2%.

That is the state #1033 left behind — minimum-of-five sampling plus the
threshold raises in #911/#1033 — and the benchmark workflow has had no
latency failure since. #908's canary, `test_tag_search_match_all_latency`,
now sits at 5.8% of its 400 ms budget. So the calibration is insurance
rather than a cure: it keeps the gate's meaning machine-independent as
those margins tighten again, which is the direction they have always moved.

The same numbers say the opposite thing about the thresholds themselves —
a budget used at 2.6% cannot detect a 10x regression — but re-anchoring
them is a separate decision from how they are compared, and it is not
#908's.

Every run prints the calibration it measured, in the terminal summary and
therefore in `benchmark_output.txt` and the job summary:

```
--------------------------- benchmark calibration ----------------------------
benchmark calibration: 634.2us/rep (reference 520.0us/rep, raw ratio 1.22x) -> budget scale 1.22x
```

The nightly absolute job prints the same measurement and says it is not
being applied, because that is the one run whose reds genuinely need
disambiguating:

```
benchmark calibration: ABSOLUTE mode (FM_BENCHMARK_ABSOLUTE set) - budgets are the raw targets; machine measured 634.2us/rep vs reference 520.0us/rep (raw ratio 1.22x, NOT applied)
```

Read a red run with that line in hand. On the **nightly absolute** job a
raw ratio well above 1.0 means the runner rather than the code. On the
**calibrated** pull-request job that correction has already been applied,
so a failure there is the code whatever the ratio says.

How much to trust the number: measured across twelve fresh processes on a
contended development box, the calibration itself spans **1.33x** — the
same order as the 1.2-1.5x runner-to-runner variance it corrects, not an
order of magnitude below it. What makes that safe is the floor, not the
precision: noise can only ever hand out unearned relief, never a new red.

The cross-machine check that says the correction lands: the development box
measures 3.44x slower than the reference, and there the worst budget sits
at **88.2%** of its raw target but **25.6%** of its calibrated budget —
against **27.7%** for the same test on the reference runner. Two machines a
factor of 3.4 apart, the same utilisation once corrected.

### Where the raw targets are still checked

`FM_BENCHMARK_ABSOLUTE=1` pins the scale at 1.0, so the suite asserts the
product targets with no correction. That is what the **nightly-absolute**
job runs (see below), and it is how you reproduce a wall-clock number
locally:

```bash
FM_BENCHMARK_ABSOLUTE=1 pytest tests/benchmarks/ -m benchmark -v
```

### Re-anchoring `CALIBRATION_REFERENCE_SECONDS`

The constant is one calibration repetition's cost on a healthy
GitHub-hosted `ubuntu-latest` runner — the machine class these thresholds
were tuned against. Every run prints its own value, so re-anchoring needs
no special run: take the `benchmark calibration:` line from a few green
runs and set the constant to their middle.

The error is one-sided, which is why an approximate value is safe. Too
**high** and the scale floors at 1.0 more often, degrading to the old
absolute behaviour. Too **low** and every runner gets permanent relief and
the gate quietly weakens. Err high.

## CI Integration

`.github/workflows/benchmarks.yml` has three jobs:

| Job | Runs on | Asserts |
|-----|---------|---------|
| `Run Performance Benchmarks` | every PR to main, every push to main, manual dispatch | **calibrated** budgets |
| `Absolute Wall-Clock Targets (nightly)` | the 02:00 UTC schedule, or a manual dispatch with `absolute_targets` | the **raw** product targets (`FM_BENCHMARK_ABSOLUTE=1`) |
| `Memory Usage Benchmarks` | all of the above | megabytes, never scaled |

The split is deliberate. A pull request is gated on something its author can
influence; "does this operation meet its wall-clock target on this runner"
is still worth asking, but it is a question about the machine as much as the
code, so it is asked nightly where a red is a signal to read rather than a
merge to re-run.

Results are:
- Uploaded as artifacts (retained 90 days)
- Commented on PRs with summary
- Reported in GitHub Actions summary, with the calibration line

## Regression Detection

If a benchmark fails:

1. **Read the calibration line first**: on the nightly-absolute job a raw
   ratio well above 1.0 means the runner, not the code. On the calibrated
   job that correction has already been applied, so a failure there is the
   code.
2. **Check the diff**: What changed since last passing run?
3. **Expected impact?**: Did you add a feature that increases latency?
4. **Investigate**: Use profiling tools (cProfile, py-spy)
5. **Fix or update baseline**: Either optimize or update targets with justification

### Example Investigation

```bash
# Profile a specific test
python -m cProfile -o profile.stats -m pytest \
    tests/benchmarks/test_case_operations.py::TestCaseCreationPerformance::test_single_case_creation_latency \
    -v

# Analyze profile
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumtime').print_stats(20)"
```

## Profiling Tools

### Python Profiler (cProfile)

```bash
python -m cProfile -o profile.stats your_script.py
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumtime').print_stats(30)"
```

### Memory Profiler

```bash
pip install memory_profiler
python -m memory_profiler your_script.py
```

### Line Profiler (detailed)

```bash
pip install line_profiler
kernprof -l -v your_script.py
```

### py-spy (sampling profiler)

```bash
pip install py-spy
py-spy record -o profile.svg -- python your_script.py
```

## Baseline Management

Baselines are stored in `.github/benchmark_baselines/baseline_v1.json`.

### Updating Baselines

When legitimate changes affect performance:

1. Run benchmarks: `pytest tests/benchmarks/ -m benchmark -v`
2. Verify new results are acceptable
3. Update baseline file with new values
4. Commit with explanation:
   ```
   git commit -m "perf: update baselines after X feature

   - Case creation now includes Y, adding ~10ms
   - Memory usage increased due to Z caching"
   ```

### Baseline Version History

- `baseline_v1.json`: Initial baseline (pre-shim integration)

## Test Database

Benchmarks use SQLite in-memory for consistency:
- Fast, isolated test environment
- No external dependencies
- Representative of core logic performance

Production performance may differ with PostgreSQL - consider running
benchmarks against a PostgreSQL container for production validation.

## Future Enhancements

- [ ] Automated regression detection (compare to baselines in CI)
- [ ] Performance dashboard (Grafana/Prometheus)
- [ ] Database query profiling
- [ ] Network latency simulation
- [ ] Multi-database benchmarks (PostgreSQL vs SQLite)
- [ ] Concurrency stress tests

## Troubleshooting

### Benchmarks fail with import errors

Ensure PYTHONPATH includes the project root:
```bash
export PYTHONPATH=/path/to/faultmaven:$PYTHONPATH
pytest tests/benchmarks/ -m benchmark -v
```

### Memory tests fail with high baseline

Test process overhead may contribute. Check:
- Other running processes
- pytest plugins loading extra modules
- Run with `--no-cov` to disable coverage

### Locust can't connect to host

Ensure the FaultMaven API is running:
```bash
python -m uvicorn faultmaven.main:app --host 0.0.0.0 --port 8000
```

## Questions?

- **Why benchmark now?** Establish baseline before adding complexity
- **What if benchmarks fail?** Investigate, optimize, or update targets
- **How often to run?** Every PR + weekly scheduled run
- **What about production?** Use APM tools (New Relic, Datadog) for production monitoring
