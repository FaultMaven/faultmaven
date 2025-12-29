"""Performance benchmarks for FaultMaven.

This package contains performance benchmarks to establish baselines
and detect regressions during evolution.

Run all benchmarks:
    pytest tests/benchmarks/ -m benchmark -v

Run specific benchmark suites:
    pytest tests/benchmarks/test_case_operations.py -m benchmark -v
    pytest tests/benchmarks/test_session_operations.py -m benchmark -v
    pytest tests/benchmarks/test_memory_usage.py -m benchmark -v
"""
