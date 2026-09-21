"""Shared wall-clock budget machinery for the two timing suites.

``tests/benchmarks/`` and ``tests/performance/`` both compare measured
durations against thresholds. The calibration that cancels runner speed
(#1555), the two-number budget the #1556 ruling fixed, and the single
comparison site all live here so neither suite owns them and both are held
to the same rule.

Each suite keeps its own ``budgets.py`` table, because a budget's
provenance is the set of runs it was measured from.
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
    describe_calibration,
    measured_calibration,
    reset_calibration_cache,
    scale_was_used,
)

__all__ = [
    "ABSOLUTE_MODE_ENV",
    "CALIBRATION_REFERENCE_SECONDS",
    "MAX_REGRESSION_MULTIPLE",
    "MIN_REGRESSION_MULTIPLE",
    "Budget",
    "LatencyBudget",
    "ThroughputBudget",
    "absolute_mode",
    "assert_latency_within",
    "assert_throughput_at_least",
    "asserted_target",
    "calibration_scale",
    "collect_budgets",
    "describe_calibration",
    "measured_calibration",
    "reset_calibration_cache",
    "scale_was_used",
]
