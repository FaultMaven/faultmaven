"""The two numbers a wall-clock budget carries, and where each is asserted.

This module is the machinery; the tables live next to the suites that use
them (``tests/benchmarks/budgets.py``, ``tests/performance/budgets.py``),
because a budget's provenance is a property of the runs it was measured
from and those differ per suite.

Why a budget has two numbers
----------------------------

#1556 measured what the one number was actually doing in
``tests/benchmarks/``. Joining all 50 budgets there with what the same test
reported in green ``main`` runs, **median utilisation was 2.6% and the
highest 27.7%** — no budget was within 50% of its target. A budget used at
2.6% cannot notice a **10x** regression in that path, and the suite passes,
which is what makes it easy to miss. #1557 measured the same thing for
``tests/performance/`` and found **median utilisation 0.29%**, with four
comparisons that could not fail at all — and review found two more that
could not, so the count there was six.

The owner's ruling on #1556, inherited by #1557: **a per-PR wall-clock
budget is a regression detector, not a product target.** Three parts:

1. per-PR thresholds are re-anchored to **2-3x measured reference cost**,
   using #1555's calibration layer;
2. the raw product SLA targets **stay**, asserted strictly inside the
   ``FM_BENCHMARK_ABSOLUTE`` nightly;
3. the two coexist, because they answer different questions.

So each budget names both, and ``asserted_target`` picks by mode:

=================  ===============================  ========================
Run                Asserts                          Scaled by calibration
=================  ===============================  ========================
pull request       ``regression``                   yes (#1555)
nightly absolute   ``product_target``               no (scale pinned at 1.0)
=================  ===============================  ========================

No table here moves a product target. Every ``product_target`` in both
tables is the number that test asserted before the re-anchor, carried over
unchanged — moving one is a product decision #908's ruling reserved to the
owner, and neither #1556 nor #1557 reopened it.

What this catches, and what it gives up
---------------------------------------

**At 2-3x, a 30% regression will not fire.** #908's ruling asked the
calibration to preserve exactly that detection, and #1555's discrimination
test asserted it against the calibrated comparison. #1556 trades it away
deliberately, and it is written here rather than discovered later:

* the calibrated noise floor measured **1.07x typical and 1.33x worst**
  across fresh processes on a loaded box, so a threshold at 1.3-1.5x would
  flake and destroy the gate's credibility again — which is how #908
  started. 2-3x is the first band that clears noise with margin.
* so this gate catches **gross** regressions: an N+1, a lost index, a sync
  call on an async path, a cache that stopped caching. It does not catch
  incremental drift.
* the trade is asserted, not just described, in
  ``tests/unit/ci/test_benchmark_calibration.py::TestDiscrimination`` —
  including the column that now passes.

``reference`` is the measured cost the anchor sits above, and each table's
docstring says which runs it came from. ``LatencyBudget`` /
``ThroughputBudget`` refuse anything outside the 2-3x band at import time,
so a re-anchor that forgets to move ``reference`` fails loudly rather than
quietly widening the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .calibration import absolute_mode

#: The band the owner's #1556 ruling fixed for a per-PR regression anchor,
#: as a multiple of the measured reference cost. Enforced at import time by
#: every budget below, so the table cannot drift back out of it silently —
#: which is the failure mode #1556 exists to fix (a budget nobody dares
#: change becomes a budget nobody can justify).
MIN_REGRESSION_MULTIPLE = 2.0
MAX_REGRESSION_MULTIPLE = 3.0


@dataclass(frozen=True)
class Budget:
    """One benchmark's regression anchor, product target and provenance.

    Args:
        test: The test function this budget belongs to. Carried so a
            failure names it and so the guard can check the table against
            the suite rather than against itself.
        regression: The per-PR threshold, 2-3x ``reference``. Scaled by the
            machine calibration (#1555) before it is compared.
        product_target: The raw wall-clock SLA, asserted only under
            ``FM_BENCHMARK_ABSOLUTE``. Unchanged by #1556.
        reference: The p95 of this test's own statistic across the 20 green
            ``main`` runs named in the module docstring.

    Units are per-subclass: seconds for ``LatencyBudget``, operations per
    second for ``ThroughputBudget``.
    """

    test: str
    regression: float
    product_target: float
    reference: float

    def _check(self, multiple: float, direction: str) -> None:
        if self.reference <= 0 or self.regression <= 0 or self.product_target <= 0:
            raise ValueError(f"{self.test}: every budget number must be positive")
        if not MIN_REGRESSION_MULTIPLE <= multiple <= MAX_REGRESSION_MULTIPLE:
            raise ValueError(
                f"{self.test}: regression anchor is {multiple:.2f}x its measured "
                f"reference, outside the {MIN_REGRESSION_MULTIPLE}-"
                f"{MAX_REGRESSION_MULTIPLE}x band #1556 fixed. Re-measure the "
                "reference from green main runs and move both together."
            )
        # ‼ Two adjacent floats whose meanings differ is exactly the pair a
        # call site swaps, so the relation between them is asserted rather
        # than trusted. It is not only a swap detector: a regression anchor
        # that has grown LOOSER than the product target means the operation
        # no longer meets its commitment with 2-3x headroom, which is news
        # and should stop the build rather than be absorbed.
        if not self._ordered():
            raise ValueError(
                f"{self.test}: the regression anchor must be {direction} than "
                f"the product target (got regression={self.regression}, "
                f"product_target={self.product_target}) — either the two are "
                "swapped, or this operation has outgrown its product target."
            )

    def _ordered(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True)
class LatencyBudget(Budget):
    """Seconds. Lower is better, so the anchor is ``reference`` x multiple."""

    def __post_init__(self) -> None:
        self._check(self.regression / self.reference, "no looser (smaller)")

    def _ordered(self) -> bool:
        return self.regression <= self.product_target


@dataclass(frozen=True)
class ThroughputBudget(Budget):
    """Operations per second. Higher is better, so the anchor DIVIDES.

    Getting the direction wrong here is silent: a floor set at
    ``reference * 2.5`` would demand 2.5x the measured rate and fail every
    run, which at least fails loudly — but one set at ``reference / 2.5``
    read as a latency budget would not. The band check below uses
    ``reference / regression`` for that reason.
    """

    def __post_init__(self) -> None:
        self._check(self.reference / self.regression, "no looser (larger)")

    def _ordered(self) -> bool:
        return self.regression >= self.product_target


def asserted_target(budget: Budget) -> Tuple[float, str]:
    """Which of a budget's two numbers this run asserts, and its name.

    The one place the per-PR / nightly split is decided. The calibration
    scale is applied by the caller and is pinned at 1.0 in absolute mode, so
    ``product_target`` reaches the comparison raw, which is the whole point
    of the nightly job.
    """
    if absolute_mode():
        return budget.product_target, "product target"
    return budget.regression, "regression budget"


def collect_budgets(namespace: Dict[str, object]) -> Dict[str, Budget]:
    """Every ``Budget`` in a table module's namespace, by constant name.

    Built from the module's own globals so a table cannot fall behind the
    constants it declares — the direction that can actually drift is the
    other one (a budget nobody applies), and that is what the guard in
    ``tests/unit/ci/test_benchmark_calibration.py`` checks.
    """
    return {
        name: value
        for name, value in sorted(namespace.items())
        if isinstance(value, Budget)
    }
