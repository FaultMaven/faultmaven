"""Shared wall-clock budget machinery for the two timing suites.

``tests/benchmarks/`` and ``tests/performance/`` both compare measured
durations against thresholds. The calibration that cancels runner speed
(#1555), the two-number budget the #1556 ruling fixed, and the single
comparison site all live here so neither suite owns them and both are held
to the same rule.

Each suite keeps its own ``budgets.py`` table, because a budget's
provenance is the set of runs it was measured from.

``record.py`` hangs off the same single comparison site: under
``FM_WALLCLOCK_RECORD`` it writes every comparison's measured statistic to
a JSONL file, which is what the benchmark workflow's A/B job (#1567)
compares between the base and the head. Unset, it does nothing. Both
suites write; only ``tests/benchmarks/`` is compared, because that is the
tree the A/B job runs.

Two instruments for timing questions a budget is the wrong tool for
(#1579), both used OUTSIDE the two suites, in the required gates:

* ``growth.py`` — ``assert_linear_growth``, for a guard whose question is a
  SHAPE (is this regex linear?). A ratio between two input sizes cancels
  machine speed on its own; this makes it cancel scheduler noise too.
* ``virtual_time.py`` — an event loop whose clock advances only when it is
  idle, for a test whose question is an ORDERING (did the ladder stop
  before the deadline?). Its elapsed figures are the same under any load.
"""

from .assertions import assert_latency_within, assert_throughput_at_least
from .budgets import (
    MAX_REGRESSION_MULTIPLE,
    MIN_REGRESSION_MULTIPLE,
    Budget,
    LatencyBudget,
    ThroughputBudget,
    asserted_target,
    collect_budgets,
)
from .calibration import (
    ABSOLUTE_MODE_ENV,
    CALIBRATION_REFERENCE_SECONDS,
    absolute_mode,
    calibration_scale,
    calibration_state,
    describe_calibration,
    measured_calibration,
    reset_calibration_cache,
    restore_calibration_state,
    scale_was_used,
)
from .growth import assert_linear_growth, growth_bound, growth_ratio
from .record import (
    LATENCY_METRIC,
    RECORD_ENV,
    THROUGHPUT_METRIC,
    record_comparison,
)
from .virtual_time import (
    VirtualTimeLoop,
    VirtualTimePolicy,
    virtual_now,
    virtual_time_module,
)

__all__ = [
    "ABSOLUTE_MODE_ENV",
    "CALIBRATION_REFERENCE_SECONDS",
    "LATENCY_METRIC",
    "MAX_REGRESSION_MULTIPLE",
    "MIN_REGRESSION_MULTIPLE",
    "RECORD_ENV",
    "THROUGHPUT_METRIC",
    "Budget",
    "LatencyBudget",
    "ThroughputBudget",
    "VirtualTimeLoop",
    "VirtualTimePolicy",
    "absolute_mode",
    "assert_latency_within",
    "assert_linear_growth",
    "assert_throughput_at_least",
    "asserted_target",
    "calibration_scale",
    "calibration_state",
    "collect_budgets",
    "describe_calibration",
    "growth_bound",
    "growth_ratio",
    "measured_calibration",
    "record_comparison",
    "reset_calibration_cache",
    "restore_calibration_state",
    "scale_was_used",
    "virtual_now",
    "virtual_time_module",
]
