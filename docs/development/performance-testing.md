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
locust -f tests/load/locustfile.py --host=http://localhost:8000
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

## CI Integration

Benchmarks run automatically on:
- Every PR to main
- Every push to main
- Weekly (Sundays at 2 AM UTC)
- Manual trigger via workflow_dispatch

Results are:
- Uploaded as artifacts (retained 90 days)
- Commented on PRs with summary
- Reported in GitHub Actions summary

## Regression Detection

If a benchmark fails:

1. **Check the diff**: What changed since last passing run?
2. **Expected impact?**: Did you add a feature that increases latency?
3. **Investigate**: Use profiling tools (cProfile, py-spy)
4. **Fix or update baseline**: Either optimize or update targets with justification

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
