"""Performance overhead tests for FaultMaven.

Measures what the request-scoped logging and context machinery costs:

- context variable creation, access, copying and isolation
- logging coordinator, unified logger and performance tracker overhead
- memory behaviour of the same paths

and, since #1579, the timings that used to be asserted as raw wall clock in
unit and integration tests the same gates run:

- ``DataSanitizer`` throughput on a large document and on a batch
- the per-call cost of the tracing and PII-redaction shims when disabled
- vocabulary and timestamp extraction, and extraction beside a 64 KB
  hostile line against ``TIER1_TIMEOUT_SECONDS``

‼ Unlike ``tests/benchmarks/``, this directory IS collected by both
required CI gates (``-m "not benchmark"`` does not exclude it), so every
wall-clock comparison here goes through
``tests/wallclock``'s ``assert_latency_within`` against a row of
``budgets.py`` — calibration-scaled so runner speed cancels, and anchored
2-3x above measured cost (#1557, inheriting #1556's ruling).

There is no opt-in env var. ``RUN_PERFORMANCE_TESTS`` used to skip nine of
these tests "to avoid CI flakiness"; the calibration is what that flag was
standing in for, and a skipped test is a budget nobody applies.

Run:
    pytest tests/performance/ -v
"""
