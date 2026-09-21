"""The single site where a measured latency or rate meets its budget.

Two suites compare wall-clock numbers against thresholds —
``tests/benchmarks/`` (its own workflow, excluded from both required CI
gates) and ``tests/performance/`` (collected by BOTH required gates). Both
go through the two functions below, so the machine-throughput calibration
(#1555) and the #1556 regression/product-target split apply identically to
each, and so widening either is one edit rather than a search.

Before #908 the benchmark comparison was written three ways in three
modules, which is the only reason calibrating it was a multi-file change;
#1557 found ``tests/performance/`` carrying **27** more hand-rolled
spellings, none of which the first version of the scan could see because
its vocabulary was built from the benchmark suite's names.
``tests/unit/ci/test_benchmark_calibration.py`` scans both trees and fails
if a further spelling appears.

Being the one site is also what makes #1567's A/B possible without a
per-suite vocabulary: each function records the statistic it was handed
(``record.py``) before asserting it, so nothing has to teach the
recorder a suite's spellings and it cannot fall behind a new test.

‼ Recorded is not compared. Both suites write records, but the A/B job
runs ``pytest tests/benchmarks/`` only, so ``tests/performance/``'s
comparisons are recorded under ``FM_WALLCLOCK_RECORD`` and no job ever
diffs them. Pointing the A/B at that tree as well is a separate decision
— it would widen what the median is taken over, which the #1567 null
experiment measured at 50 benchmarks a side.
"""

from typing import Tuple

from .budgets import Budget, LatencyBudget, ThroughputBudget, asserted_target
from .calibration import calibration_scale
from .record import LATENCY_METRIC, THROUGHPUT_METRIC, record_comparison


def _threshold(budget: Budget, expected: type) -> Tuple[float, str, float]:
    """The number to compare against, what to call it, and the scale used.

    One place decides both halves of the split: ``asserted_target`` picks
    the per-PR anchor or the nightly's raw product target, and
    ``calibration_scale`` supplies the machine correction (pinned at 1.0 in
    absolute mode, so the product target reaches the comparison raw).

    ``expected`` is checked because a latency budget and a throughput
    budget are both a pair of floats, and passing one to the other helper
    would invert the direction silently — the exact failure
    ``assert_throughput_at_least``'s docstring warns about.
    """
    if not isinstance(budget, expected):
        raise TypeError(
            f"expected a {expected.__name__}, got "
            f"{type(budget).__name__} ({getattr(budget, 'test', budget)!r})"
        )
    scale = calibration_scale()
    target, kind = asserted_target(budget)
    return target, kind, scale


def assert_latency_within(
    observed_seconds: float,
    budget: LatencyBudget,
    label: str,
    detail: str = "",
) -> None:
    """Compare one latency against its budget, calibration-scaled.

    ‼ This is the ONLY place in either suite where a measured latency is
    compared against a threshold. Before #908 the comparison was written out
    three times — ``measured.best < 0.200`` inline here,
    ``stats["p95_ms"] < 200`` inline in ``test_case_service_operations``, and
    a bool returned by ``report_benchmark`` in
    ``test_investigation_session_service_operations`` — which is why
    calibrating "the benchmark assertion" meant finding all three. #1557
    then found 27 more in ``tests/performance/``. Route new sites here;
    ``tests/unit/ci/test_benchmark_calibration.py`` scans both trees and
    fails if a further spelling appears.

    WHICH number it compares against is the #1556 split: the budget's
    ``regression`` anchor on a pull request, its ``product_target`` under
    ``FM_BENCHMARK_ABSOLUTE``. Both live in ``budgets.py`` with the
    measurement they came from.

    Args:
        observed_seconds: The measured statistic, in seconds.
        budget: This test's row in its suite's ``budgets.py``. Both of its
            numbers are
            written as product/anchor seconds, NOT pre-scaled — the
            scaling happens here and nowhere else.
        label: What was measured, for the failure message.
        detail: Optional extra context (a distribution, a row count).
    """
    target_seconds, kind, scale = _threshold(budget, LatencyBudget)
    limit = target_seconds * scale
    suffix = f" {detail}" if detail else ""
    # Before the assert, so a site that is already over its absolute
    # budget still contributes its number to the A/B (#1567).
    record_comparison(
        metric=LATENCY_METRIC,
        label=label,
        observed=observed_seconds,
        budget=target_seconds,
        kind=kind,
        scale=scale,
    )
    assert observed_seconds < limit, (
        f"{label}: {observed_seconds * 1000:.1f}ms exceeds "
        f"{limit * 1000:.1f}ms budget "
        f"({target_seconds * 1000:.0f}ms {kind} x {scale:.2f} calibration)"
        f"{suffix}"
    )


def assert_throughput_at_least(
    observed_per_second: float,
    budget: ThroughputBudget,
    label: str,
    detail: str = "",
) -> None:
    """Throughput counterpart of ``assert_latency_within``.

    Throughput is 1/latency, so a machine running ``scale`` times slower
    clears a floor that is ``scale`` times LOWER. Dividing rather than
    multiplying is the whole difference, and getting it backwards would
    tighten the floor on exactly the runners this exists to relieve.

    The #1556 split applies here too, inverted: the ``regression`` floor is
    the HIGHER of the budget's two numbers, because a tighter throughput
    floor is a larger one.
    """
    target_per_second, kind, scale = _threshold(budget, ThroughputBudget)
    floor = target_per_second / scale
    suffix = f" {detail}" if detail else ""
    record_comparison(
        metric=THROUGHPUT_METRIC,
        label=label,
        observed=observed_per_second,
        budget=target_per_second,
        kind=kind,
        scale=scale,
    )
    assert observed_per_second > floor, (
        f"{label}: {observed_per_second:.1f}/s below "
        f"{floor:.1f}/s floor "
        f"({target_per_second:.0f}/s {kind} / {scale:.2f} calibration)"
        f"{suffix}"
    )
