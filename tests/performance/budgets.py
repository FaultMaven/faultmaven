"""The per-PR regression anchor and product target for every timed check here.

The machinery — the two-number ``Budget`` dataclasses, the 2-3x band they
enforce at import time, and ``asserted_target``'s per-PR / nightly split —
lives in ``tests/wallclock/budgets.py``, shared with ``tests/benchmarks/``.
That module's docstring carries the #1556 ruling and what a 2-3x anchor
gives up. This module carries the table and where its numbers came from.

Why this directory has a table at all (#1557)
---------------------------------------------

``tests/benchmarks/`` is excluded from both required CI gates by
``-m "not benchmark"`` and runs in a job of its own. **This directory is
not.** ``Test Standalone`` (``-m "not cloud and not benchmark"``) and
``Test Cloud`` (``-m "not benchmark"``) both collect it, so a wall-clock
assertion here reds a REQUIRED check on a diff that changed nothing —
#908's failure, except blocking the merge.

#1557 measured all 27 hand-rolled comparisons that were here, over 29 runs
on the development box:

* **median utilisation 0.29%**, highest 16.7%. A budget used at 0.3%
  cannot notice a 100x regression, and the suite passes.
* **six comparisons could not fail at all** — four for the reason in
  "What was deleted", and two more that review caught in #1557's own
  first draft: see "Two budgets are one budget" below.

The first 21 rows below are what remains. After re-anchoring, median
utilisation is **30.7%** and the highest **41.9%**, which is the same
shape #1556 produced for the benchmark suite (2.5% to 34.6%). The last
seven were moved here by #1579 from tests outside this directory; their
provenance is in the comment above them.

Provenance
----------

**Anchored 2026-09-21** from **29 runs** of this suite on the FaultMaven
development box, with ``RUN_PERFORMANCE_TESTS=true`` so the nine tests that
used to skip were measured too. ``reference`` on each row is the **p95** of
that row's statistic across those runs; ``regression`` is a round number in
the 2-3x band above it. ``DEDUPLICATION_OP`` is the p95 of each run's
WORST loop iteration, because that test compares four times and all four
must pass.

Two things about that measurement, stated rather than implied:

* **It is a development box, not CI.** ``tests/performance/`` emits nothing
  to a CI artifact, so there was no runner-side number to join against the
  way #1556 joined ``benchmark-results``. The box measured **3.53x** slower
  than ``CALIBRATION_REFERENCE_SECONDS`` while these runs were taken, and
  it was running several other pytest processes throughout — so for the
  CPU-bound rows the reference is roughly 3.5x above what a healthy runner
  would report, and every anchor is correspondingly conservative. The error
  is one-sided toward relief, which is the direction #1555's own reference
  constant chose. Every test below now prints its statistic, so a later
  re-anchor from CI logs is a join rather than a re-measurement.
* **Every anchor also clears the WORST of the 29 runs**, not just their
  p95 — by 1.40x to 2.65x, except ``DEDUPLICATION_OP`` at 1.23x, the
  thinnest margin in the table and the one worth watching. Its 100-
  operation loop measures a ~0.1 ms window, short enough for a single GC
  pause to double the per-operation figure. The calibration adds its
  relief on top of all of these on any runner slower than the reference.

Product targets
---------------

Nothing here moves a product target: every ``product_target`` is the number
that site asserted before #1557, converted to seconds.

Five of them were written as a PERCENTAGE of the measured total rather than
as a duration, and those need one reading stated. ``overhead_percentage =
overhead / total * 100`` with ``total = work + overhead`` is bounded above
by 100 by construction, so ``< 100`` and ``< 1000`` cannot fail — the
author's denominator was the total where the sentence means the WORK.
Read against the work, ``< P`` means ``overhead < P/100 x work``, which is
finite and is what the two surviving rows record. The reading is not a
guess: ``test_typical_api_request_overhead`` asserts BOTH
``overhead_percentage < 50`` and ``logging_overhead < 0.05`` over a 0.1 s
workload, and 50% OF THE WORK is exactly 0.05 s. The two agree only under
this reading.

What was deleted, and why (4 of the 27)
---------------------------------------

Four comparisons are gone rather than re-anchored, because re-anchoring
them would have put a number on a quantity that is not a measurement of
this codebase. Each subtracted a NOMINAL sleep total from a measured one
and called the difference "logging overhead". Timed on the same box, a
bare loop of the same sleeps with no logging at all:

===============================================  ==========  ==========  =====
site                                             reported    bare sleep  slack
                                                 "overhead"  slack       share
===============================================  ==========  ==========  =====
``test_operation_context_manager_overhead``      26.3 ms     19.4 ms     74%
``test_high_frequency_operations``               106 ms      97.3 ms     92%
``test_async_context_propagation_overhead``      (negative)  34.4 ms     all
``test_typical_api_request_overhead`` (the %)    1.74 ms     0.3 ms      17%
===============================================  ==========  ==========  =====

So ``test_operation_context_manager_overhead``, the ONE comparison in this
directory that ran near its threshold (44% of it), was three-quarters
event-loop timer granularity — a spurious-red generator sitting in both
required gates. ``test_high_frequency_operations`` and
``test_async_context_propagation_overhead`` could not fail at all, and the
latter also computed its expected work serially for tasks that run
concurrently, which is why its percentage came out at **-1250%**.
Subtracting a MEASURED baseline instead does not rescue them: the real
overhead is smaller than the run-to-run spread of either term.
``test_typical_api_request_overhead`` keeps its duration assertion
(``API_REQUEST_LOGGING_OVERHEAD``, 17% slack over a single 100 ms sleep)
and loses only the percentage, which was a looser second view of the same
number.

All four tests keep their correctness assertions and still print their
timings.

Two budgets are one budget (2 more of the 27)
---------------------------------------------

``test_context_isolation_performance`` and
``test_context_under_high_concurrency`` each reported a per-task time AND
a per-operation time, and #1557's first draft gave each of the four a
budget. But ``avg_operation_time`` is exactly ``avg_task_time /
operations_per_task`` — one measurement in two units — so the pair is one
constraint written twice, and the looser half can never fire first. As
shipped in that draft: ``1.8e-5 x 20 = 3.6e-4`` against a ``3.5e-4``
per-task anchor, and ``3.5e-4 x 20 = 0.007`` against ``0.007``. Both
per-operation rows are gone; both numbers are still printed.

Nothing in this table records what statistic a row judges, so no check
can see that mechanically. What
``tests/unit/ci/test_benchmark_calibration.py`` does instead is refuse
the situation quietly: a test carrying two budgets has to name the two
independent timed windows they come from, in
``INDEPENDENT_MEASUREMENTS``. A rescaling has no honest entry to write.
"""

from __future__ import annotations

from typing import Dict

from faultmaven.modules.preprocessing.preprocessing_service import (
    TIER1_TIMEOUT_SECONDS,
)
from tests.wallclock.budgets import (
    Budget,
    LatencyBudget,
    ThroughputBudget,
    collect_budgets,
)

__all__ = ["Budget", "LatencyBudget", "ThroughputBudget", "ALL_BUDGETS"]


# --- tests/performance/test_context_overhead.py ------------------------------

CONTEXT_GET = LatencyBudget(
    "test_context_variable_access_speed",
    regression=1.0e-6,
    product_target=1.0e-5,
    reference=3.6903e-07,
)
CONTEXT_SET = LatencyBudget(
    "test_context_variable_access_speed",
    regression=1.0e-5,
    product_target=5.0e-5,
    reference=4.9618e-06,
)
CONTEXT_COPY = LatencyBudget(
    "test_context_copying_performance",
    regression=1.0e-6,
    product_target=0.010,
    reference=3.9065e-07,
)
CONTEXT_COPY_EXEC = LatencyBudget(
    "test_context_copying_performance",
    regression=1.8e-6,
    product_target=0.010,
    reference=8.2979e-07,
)
CONCURRENT_CONTEXT_OP = LatencyBudget(
    "test_concurrent_context_access",
    regression=3.0e-5,
    product_target=0.100,
    reference=1.4234e-05,
)
ISOLATED_TASK = LatencyBudget(
    "test_context_isolation_performance",
    regression=0.007,
    product_target=10.0,
    reference=0.0030252,
)
#: Not a latency: the spread between the fastest and slowest task's mean
#: operation time. It is still a duration, it still scales with the
#: machine, and the ruling's question — regression detector or product
#: target — has the same answer for it.
ISOLATION_TIME_SPREAD = LatencyBudget(
    "test_context_isolation_performance",
    regression=1.6e-5,
    product_target=0.100,
    reference=7.6929e-06,
)
CONTEXT_SWITCH = LatencyBudget(
    "test_rapid_context_switching",
    regression=4.0e-6,
    product_target=1.0e-4,
    reference=1.7984e-06,
)
HIGH_CONCURRENCY_TASK = LatencyBudget(
    "test_context_under_high_concurrency",
    regression=3.5e-4,
    product_target=0.100,
    reference=1.7181e-04,
)
LARGE_DATA_CHECK = LatencyBudget(
    "test_context_with_large_data",
    regression=1.0e-4,
    product_target=0.010,
    reference=3.6082e-05,
)

# --- tests/performance/test_logging_overhead.py ------------------------------

REQUEST_CONTEXT_CREATE = LatencyBudget(
    "test_request_context_creation_overhead",
    regression=2.4e-5,
    product_target=0.010,
    reference=1.0033e-05,
)
COORDINATOR_CYCLE = LatencyBudget(
    "test_logging_coordinator_overhead",
    regression=1.0e-4,
    product_target=0.020,
    reference=4.0215e-05,
)
TRACKER_RECORD = LatencyBudget(
    "test_performance_tracker_overhead",
    regression=3.5e-6,
    product_target=0.001,
    reference=1.5947e-06,
)
UNIFIED_LOGGER_SET = LatencyBudget(
    "test_unified_logger_overhead",
    regression=4.0e-4,
    product_target=0.005,
    reference=1.9172e-04,
)
#: The two rows below subtract a MEASURED un-logged loop rather than a
#: nominal sleep total, which is why they survived where the four in "What
#: was deleted" did not: the timer slack is in both terms and cancels.
#: Their product target is the percentage they asserted read against the
#: work — ``< 100%`` of a 0.5 s / 0.15 s workload.
SERVICE_LOGGING_OVERHEAD = LatencyBudget(
    "test_service_operation_logging_overhead",
    regression=0.060,
    product_target=0.500,
    reference=0.026304,
)
EXTERNAL_CLIENT_LOGGING_OVERHEAD = LatencyBudget(
    "test_external_client_logging_overhead",
    regression=0.060,
    product_target=0.150,
    reference=0.028320,
)
CONCURRENT_REQUEST = LatencyBudget(
    "test_concurrent_logging_performance",
    regression=0.003,
    product_target=0.100,
    reference=0.0013988,
)
CONTEXT_VARIABLE_ITERATION = LatencyBudget(
    "test_context_variable_performance",
    regression=1.5e-4,
    product_target=0.005,
    reference=6.1121e-05,
)
#: The thinnest margin in this table: 1.23x the worst of the 29 runs,
#: because the 100-operation iteration of this test's loop measures a
#: ~0.1 ms window. ``reference`` is the p95 of each run's WORST iteration,
#: not of all four, because the test asserts all four.
DEDUPLICATION_OP = LatencyBudget(
    "test_deduplication_scaling",
    regression=5.0e-6,
    product_target=1.0e-4,
    reference=1.9191e-06,
)
LOG_ONCE_CALL = LatencyBudget(
    "test_log_once_performance",
    regression=3.5e-6,
    product_target=5.0e-4,
    reference=1.7240e-06,
)
API_REQUEST_LOGGING_OVERHEAD = LatencyBudget(
    "test_typical_api_request_overhead",
    regression=0.006,
    product_target=0.050,
    reference=0.0028241,
)


# --- moved here from the required-gate unit tests (#1579) --------------------
#
# Anchored 2026-09-26 from 20 runs of the three modules below on the same
# development box as the rows above, under its usual load (load average 7-10
# from other lanes' pytest processes). ``reference`` is the p95 of the
# statistic across those runs — the p5 for the two throughput rows, where
# the slow side is the low one — and the anchor is a round number in the
# 2-3x band above it. Every anchor also clears the WORST of the 20 runs, by
# 2.0x to 2.4x. Every ``product_target`` is the number the moved assertion
# used, converted to this row's units:
#
# =============================  ====================================  =========
# row                            asserted before, and where            product
# =============================  ====================================  =========
# SANITIZE_LARGE_DOCUMENT        ``lines_per_second > 100``            100/s
#                                (and ``processing_time < 10.0`` for
#                                1000 lines: the same constraint)
# SANITIZE_DOCUMENT_BATCH        ``documents_per_second > 10``         10/s
# NOOP_TRACK_CALL                1000 calls ``< 0.1`` s                100 us
# PII_PASSTHROUGH_CALL           1000 calls ``< 0.05`` s               50 us
# VOCABULARY_EXTRACTION          ``< 0.5`` s on ~1 MB                  0.5 s
# TIMESTAMP_EXTRACTION           1000 calls ``< 1.0`` s                1 ms
# ADVERSARIAL_LINE_EXTRACTION    ``elapsed < TIER1_TIMEOUT_SECONDS``   2.0 s
# =============================  ====================================  =========
#
# ``ADVERSARIAL_LINE_EXTRACTION`` is the one row near its product target:
# the worst shape, ``sshd-word-chain``, has a p95 of 0.93 s on this box
# against a 2.0 s Tier-1 timeout, so its anchor sits AT the product target
# (2.16x reference) rather than inside it. It is one row for eleven shapes,
# anchored on the worst of each run as ``DEDUPLICATION_OP`` is on its worst
# iteration, which means it is a loose anchor for the nine cheap shapes.
# The two expensive ones are expensive because the extractor is
# super-linear on them (#1700) — see ``tests/performance/test_extraction_speed.py``.

# tests/performance/test_sanitization_throughput.py
SANITIZE_LARGE_DOCUMENT = ThroughputBudget(
    "test_large_document_sanitization_throughput",
    regression=12000.0,
    product_target=100.0,
    reference=28514.0,
)
SANITIZE_DOCUMENT_BATCH = ThroughputBudget(
    "test_document_batch_sanitization_throughput",
    regression=2500.0,
    product_target=10.0,
    reference=5569.8,
)

# tests/performance/test_shim_overhead.py
NOOP_TRACK_CALL = LatencyBudget(
    "test_noop_decorator_minimal_overhead",
    regression=8.0e-7,
    product_target=1.0e-4,
    reference=3.2035e-07,
)
PII_PASSTHROUGH_CALL = LatencyBudget(
    "test_pii_redactor_passthrough_minimal_overhead",
    regression=6.5e-7,
    product_target=5.0e-5,
    reference=2.6420e-07,
)

# tests/performance/test_extraction_speed.py
VOCABULARY_EXTRACTION = LatencyBudget(
    "test_vocabulary_extraction_on_a_megabyte_of_logs",
    regression=0.045,
    product_target=0.5,
    reference=0.018410,
)
TIMESTAMP_EXTRACTION = LatencyBudget(
    "test_timestamp_extraction_per_line",
    regression=1.2e-5,
    product_target=1.0e-3,
    reference=5.2370e-06,
)
ADVERSARIAL_LINE_EXTRACTION = LatencyBudget(
    "test_an_adversarial_line_extracts_inside_the_tier1_timeout",
    regression=2.0,
    product_target=TIER1_TIMEOUT_SECONDS,
    reference=0.92524,
)


#: Every budget in this table, by constant name.
ALL_BUDGETS: Dict[str, Budget] = collect_budgets(globals())
