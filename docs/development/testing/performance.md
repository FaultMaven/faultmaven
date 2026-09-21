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

## Where the numbers live

Every benchmark in `tests/benchmarks/` carries **two** thresholds, and they
answer different questions (#1556):

| | Asserted by | Value | Scaled by the calibration |
|---|---|---|---|
| **Regression anchor** | every pull request and push to `main` | 2-3x that operation's measured cost on CI | yes |
| **Product target** | the nightly `FM_BENCHMARK_ABSOLUTE` job only | the wall-clock SLA | no |

Both are in **`tests/benchmarks/budgets.py`**, one row per benchmark, each
recording the p95 it was anchored from. That file is the source of truth —
this guide deliberately does not restate 50 numbers, because the copy is
what goes stale.

Memory thresholds are the exception: they are plain assertions in
`test_memory_usage.py`, never calibrated and never re-anchored, because
megabytes do not move with machine throughput.

| Metric | Target | Measurement |
|--------|--------|-------------|
| Baseline RSS | < 1500MB | `test_memory_usage_baseline` |
| RSS under load | < 2000MB | `test_memory_usage_under_load` |
| Growth after GC | < 100MB | `test_memory_cleanup_after_gc` |

## The other timed suite: `tests/performance/`

`tests/performance/` measures logging and context-variable overhead, and
the thing to know about it is **where it runs**:

| | `tests/benchmarks/` | `tests/performance/` |
|---|---|---|
| `Test Standalone` (`-m "not cloud and not benchmark"`) | excluded | **collected** |
| `Test Cloud` (`-m "not benchmark"`) | excluded | **collected** |
| its own workflow | `benchmarks.yml` | none |

So a wall-clock threshold there reds a **required** check on a diff that
changed nothing — #908's failure with the merge blocked (#1557). It
carries its own `tests/performance/budgets.py` on the same two-number
shape, anchored 2-3x above 29 measured runs, and the shared machinery both
suites use lives in **`tests/wallclock/`**:

| module | what it holds |
|---|---|
| `tests/wallclock/calibration.py` | the machine-throughput measurement (#908/#1555) |
| `tests/wallclock/budgets.py` | the `Budget` dataclasses, the 2-3x band, `asserted_target` |
| `tests/wallclock/assertions.py` | `assert_latency_within`, `assert_throughput_at_least` |

Four of the 27 comparisons that used to be in `tests/performance/` were
**deleted** rather than re-anchored: each subtracted a nominal
`asyncio.sleep` total from a measured one, and a bare loop of the same
sleeps with no instrumentation accounts for 74-100% of the result.
`tests/performance/budgets.py` carries that measurement.

There is no `RUN_PERFORMANCE_TESTS` flag any more. It skipped nine of
these tests to avoid CI flakiness; the calibration is what it was standing
in for, and a skipped test is a budget nobody applies.

## Keeping the comparison in one place

Two checks in `tests/unit/ci/test_benchmark_calibration.py` hold both
directories to the helpers, and they fail on different things:

1. **A shape scan.** Any ordering comparison against a bound — literal on
   either side, a named constant, a chain, negated, `assertLess`,
   `operator.lt`, or `if … : raise` — is a hand-rolled threshold unless it
   is in `THRESHOLD_ALLOWLIST`, which has nine entries and a reason on
   each. It keys on the shape rather than on variable names because the
   name-based version scored **0 against 27 live violations** the first
   time it was pointed at `tests/performance/`.
2. **A reachability check.** Every test that takes a clock reading must
   reach `assert_latency_within` / `assert_throughput_at_least` through
   the call graph, by any route. This is the one a new spelling cannot
   walk past, and `UNJUDGED_TIMED_TESTS` names the three deliberate
   exceptions with their measurements.

Known limit, measured: both watch two directories. **59 timed tests in 18
other files under `tests/` carry 50 threshold comparisons** that neither
reaches, and they run in the same two required gates.

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

So the thresholds stayed (#908's ruling: they encode product targets) and
the instrument changed. `tests/wallclock/calibration.py` measures a fixed,
cheap, CPU-bound workload **in the same pytest process**, and every budget
is scaled by it:

```
budget      = threshold     x scale
floor       = threshold     / scale        (throughput: it is 1/latency)
scale       = max(1.0, measured / CALIBRATION_REFERENCE_SECONDS)
```

`threshold` was the product target until #1556 re-anchored the per-PR side;
it is now whichever of the budget's two numbers this run asserts (below).
The scaling itself is unchanged.

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
  `tests/unit/ci/test_benchmark_calibration.py`. How big a single-path
  regression has to be is set by the anchoring, not by the calibration —
  see #1556 below.
* **Memory assertions are not scaled.** Megabytes do not move with machine
  throughput, and correcting them would be nonsense.

### A budget is a regression detector, not a product target (#1556)

Once #1555 made the comparison machine-independent, the *other* side of
#908 was fully exposed. Joining all **50** budgets with the median each
test reported across 20 green `main` runs:

| | before #1556 | after |
|---|---|---|
| median utilisation | **2.5%** | **34.6%** |
| highest utilisation | 24.5% | 40.8% |
| budgets within 50% of target | 0 | 0 |

(#1556 reported 2.6% and 2.2% median from two single runs; the table above
is the same join over 20, which is also what the anchors came from.)

A budget used at 2.5% cannot notice a **10x** regression in that path, and
the suite passes, which is what makes it easy to miss. #908's canary,
`test_tag_search_match_all_latency`, sat at 5.7% of its 400 ms budget and
now sits at 37.9% of a 60 ms one.

The owner's ruling on #1556 resolved it: **a per-PR benchmark budget is a
regression detector, not a product target.** The per-PR thresholds were
re-anchored to 2-3x measured cost, the raw product SLAs stayed and now live
strictly inside the nightly, and both numbers sit side by side in
`tests/benchmarks/budgets.py`.

#### What this catches, and what it gives up

**At 2-3x, a 30% regression will not fire.** #908's ruling asked the
calibration to preserve exactly that detection and #1555's discrimination
test asserted it; #1556 traded it away deliberately. The calibrated noise
floor measured **1.07x typical and 1.33x worst** across fresh processes on
a loaded box, so a threshold at 1.3-1.5x would flake and destroy the gate's
credibility again — which is how #908 started. 2-3x is the first band that
clears noise with margin.

So the per-PR gate catches **gross** regressions: an N+1, a lost index, a
sync call on an async path, a cache that stopped caching. It does not catch
incremental drift. That is asserted rather than described —
`TestDiscrimination` in `tests/unit/ci/test_benchmark_calibration.py` now
has a column that checks a 30% regression **passes**.

Where 30% sensitivity would have to live is the nightly, against a raw
target on a quiet runner. Note honestly what that costs today: the product
targets sit **3.6x to 172x** above measured cost, median **35x**
(`product_target / reference` per row of the table), so the nightly as it
stands answers "does the wall clock still meet the commitment", not "did
anything get 30% slower". Making it answer the second means tightening a
product target, which is an owner decision #908's ruling reserved and #1556
did not reopen.

#### Where the anchors came from

**2026-09-20**, from the `benchmark-results` artifact of **20 green `main`
runs** spanning 2026-09-19T18:35Z to 2026-09-20T12:10Z. The run ids are
recorded in `budgets.py`'s module docstring, and each row's `reference` is
the p95 across those runs of the statistic that row's test compares.

Two things about that choice, both deliberate:

* **The anchor is observed CI cost, not `CALIBRATION_REFERENCE_SECONDS`.**
  That constant is derived (development box x an artifact ratio, rounded
  up) and #1555's review found it errs roughly 8% toward relief on the
  lane's own cross-machine numbers. Anchoring 2-3x on top of it would have
  baked that error into 50 budgets at once.
* **It is the p95 of the raw reported statistic**, with no calibration
  applied — 19 of the 20 runs predate #1555 and carry no calibration line,
  and the raw p95 already sits at the slow end of the runner distribution.
  A slow runner then gets the calibration's relief on top, so the error is
  one-sided in the safe direction. Measured: every anchor sits at least
  **2.10x** above the *worst* of those 20 runs, not just above their p95.

`budgets.py` refuses at import time to hold an anchor outside the 2-3x
band, so a re-anchor that moves a threshold and forgets its `reference`
fails loudly instead of quietly widening the gate.

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

The cross-machine check that says the correction lands, re-measured on the
re-anchored budgets (2026-09-20): the development box measures **3.56x**
slower than the reference, and running the whole suite there gives a
calibrated utilisation of **median 31.3%, max 40.0%** — against the
**34.6% / 40.8%** the same anchors project on the reference runner from
the 20-run join. Two machines a factor of 3.6 apart, the same utilisation
to within about three points, which is exactly what "the threshold stops
measuring which machine you got" means. (#1555 made the same check against
the pre-#1556 budgets: worst budget at 88.2% of its raw target but 25.6%
of its calibrated one, against 27.7% on the runner.)

The same suite in absolute mode on that box: median **9.4%** of the raw
product targets, max 65.5%, nothing red. The nightly is not measuring the
same thing, and this is what that difference looks like.

### Where the raw targets are still checked

`FM_BENCHMARK_ABSOLUTE=1` switches every comparison from the budget's
regression anchor to its `product_target`, and pins the scale at 1.0, so
the suite asserts the raw product targets with no correction. That is what
the **nightly-absolute** job runs (see below), and it is how you reproduce
a wall-clock number locally:

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
| `Run Performance Benchmarks` | every PR to main, every push to main, manual dispatch | **calibrated regression anchors** (2-3x measured cost) |
| `Absolute Wall-Clock Targets (nightly)` | the 02:00 UTC schedule, or a manual dispatch with `absolute_targets` | the **raw product targets** (`FM_BENCHMARK_ABSOLUTE=1`) |
| `Memory Usage Benchmarks` | all of the above | megabytes, never scaled |

The split is deliberate, and #1556 sharpened it into two different
questions rather than one question asked twice. A pull request is gated on
"did this change make something grossly slower", which is about the code
and nothing else — so it compares a machine-corrected anchor sitting just
above measured cost. "Does this operation meet its wall-clock target" is a
question about the machine as much as the code, so it is asked nightly,
uncorrected, where a red is a signal to read rather than a merge to
re-run.

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
5. **Fix, or re-anchor deliberately**: optimise the path, or — if the new
   cost is the intended one — re-measure and move that budget's
   `regression` and `reference` together (recipe in `budgets.py`'s module
   docstring). Moving a `product_target` is a separate, owner-level
   decision.

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
