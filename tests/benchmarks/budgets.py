"""The two numbers every benchmark budget carries, and where each is asserted.

Why a budget has two numbers
----------------------------

#1556 measured what the one number was actually doing. Joining all 50
budgets in this suite with what the same test reported in green ``main``
runs, **median utilisation was 2.6% and the highest 27.7%** — no budget was
within 50% of its target. A budget used at 2.6% cannot notice a **10x**
regression in that path, and the suite passes, which is what makes it easy
to miss.

The owner's ruling on #1556: **a per-PR benchmark budget is a regression
detector, not a product target.** Three parts:

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

Nothing here moves a product target. Every ``product_target`` below is the
number that test asserted before #1556, carried over unchanged — moving one
is a product decision #908's ruling reserved to the owner, and #1556's
ruling did not reopen it.

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

Where 30% sensitivity would have to live is the nightly, against a raw
target. Note honestly what that costs today: the shipped product targets
sit **3.6x to 172x** above measured cost, median **35x**
(``product_target / reference`` per row below), so the nightly as it stands
answers "does the wall clock still meet the commitment", not "did anything
get 30% slower". Making it answer the second means tightening a product
target, which is the owner's call.

Provenance
----------

**Anchored 2026-09-20** from the ``benchmark-results`` artifact of **20
green ``main`` runs** of ``.github/workflows/benchmarks.yml``, spanning
2026-09-19T18:35Z to 2026-09-20T12:10Z:

    35461677770 35474672238 35475059488 35475097524 35484369920
    35484550611 35485019695 35485662738 35496672222 35499493567
    35499497362 35499500793 35499503672 35499506877 35499510079
    35509856922 35509860447 35509863290 35509866821 35509869701

``reference`` on each row is the **p95 across those 20 runs** of the
statistic that row's test compares — the minimum for a
``measure_min_latency`` site, the p95 for a ``report_p95`` /
``report_benchmark`` one, the rate for a throughput one. ``regression`` is
a round number in the 2-3x band above it.

The anchor is **observed CI cost, not** ``CALIBRATION_REFERENCE_SECONDS``.
That constant is derived (development box x an artifact ratio, rounded up)
and #1555's review found it errs roughly 8% toward relief on the lane's own
cross-machine numbers; anchoring 2-3x on top of it would bake that error
into 50 budgets at once.

It is also deliberately the p95 of the **raw** reported statistic, with no
calibration applied — 19 of the 20 runs predate #1555 and carry no
calibration line, and the raw p95 already sits at the slow end of the
runner distribution. A slow runner then gets the calibration's relief on
top, so the error is one-sided in the safe direction.

Re-anchoring, later
-------------------

The whole point of recording the runs is that this is repeatable rather
than negotiated. Download N green ``main`` runs and join::

    gh run download <run-id> -R FaultMaven/faultmaven -n benchmark-results
    jq -r '.tests[] | "\\(.nodeid)\\t\\(.call.stdout)"' benchmark_results.json

then take the p95 per test and set ``regression`` to a round number 2-3x
above it. ``LatencyBudget``/``ThroughputBudget`` refuse anything outside
that band at import time, so a re-anchor that forgets to move ``reference``
fails loudly rather than quietly widening the gate.
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


# --- tests/benchmarks/test_case_operations.py --------------------------------

CASE_CREATE = LatencyBudget(
    "test_single_case_creation_latency",
    regression=0.015,
    product_target=1.000,
    reference=0.005805,
)
CASE_CREATE_THROUGHPUT = ThroughputBudget(
    "test_batch_case_creation_throughput",
    regression=80,
    product_target=50,
    reference=179.60,
)
CASE_RETRIEVE = LatencyBudget(
    "test_single_case_retrieval_latency",
    regression=0.040,
    product_target=0.100,
    reference=0.016710,
)
CASE_LIST = LatencyBudget(
    "test_list_cases_latency",
    regression=0.040,
    product_target=0.150,
    reference=0.017245,
)
CASE_UPDATE = LatencyBudget(
    "test_case_update_latency",
    regression=0.006,
    product_target=0.150,
    reference=0.002705,
)
#: ‼ Three numbers live at this site and they are not the same number. The
#: docstring names a 200 ms product aspiration; the asserted product target
#: is the 1.0 s this test has always compared against; the regression
#: anchor is 400 ms. The 200 ms warning in the test body is the first of
#: those and is left exactly as #1555 wrote it — reconciling an aspiration
#: with an asserted target is a product decision, not this table's.
CASE_SEARCH = LatencyBudget(
    "test_search_cases_latency",
    regression=0.400,
    product_target=1.000,
    reference=0.148755,
)

# --- tests/benchmarks/test_case_service_operations.py ------------------------

CASE_SERVICE_CREATE = LatencyBudget(
    "test_create_case_performance",
    regression=0.015,
    product_target=0.200,
    reference=0.006977,
)
CASE_SERVICE_GET = LatencyBudget(
    "test_get_case_performance",
    regression=0.050,
    product_target=0.100,
    reference=0.018838,
)
CASE_SERVICE_UPDATE = LatencyBudget(
    "test_update_case_performance",
    regression=0.060,
    product_target=0.150,
    reference=0.022254,
)
CASE_SERVICE_LIST = LatencyBudget(
    "test_list_cases_performance",
    regression=0.080,
    product_target=0.300,
    reference=0.030911,
)
CASE_SERVICE_GET_WITH_DETAILS = LatencyBudget(
    "test_get_case_with_details_performance",
    regression=0.050,
    product_target=0.250,
    reference=0.019836,
)
CASE_SERVICE_STATISTICS = LatencyBudget(
    "test_get_statistics_performance",
    regression=0.500,
    product_target=1.000,
    reference=0.192949,
)
CASE_SERVICE_CLOSE = LatencyBudget(
    "test_close_case_performance",
    regression=0.060,
    product_target=0.200,
    reference=0.022393,
)

# --- tests/benchmarks/test_investigation_session_operations.py ---------------

SESSION_CREATE = LatencyBudget(
    "test_single_session_creation_latency",
    regression=0.010,
    product_target=0.200,
    reference=0.004400,
)
SESSION_CREATE_WITH_METADATA = LatencyBudget(
    "test_session_creation_with_metadata_latency",
    regression=0.012,
    product_target=0.200,
    reference=0.004600,
)
SESSION_CREATE_THROUGHPUT = ThroughputBudget(
    "test_batch_session_creation_throughput",
    regression=100,
    product_target=20,
    reference=260.50,
)
SESSION_RETRIEVE = LatencyBudget(
    "test_single_session_retrieval_latency",
    regression=0.005,
    product_target=0.100,
    reference=0.001900,
)
SESSION_GET_ACTIVE = LatencyBudget(
    "test_get_active_session_latency",
    regression=0.005,
    product_target=0.100,
    reference=0.002000,
)
SESSION_LIST_BY_CASE = LatencyBudget(
    "test_list_sessions_by_case_latency",
    regression=0.010,
    product_target=0.200,
    reference=0.004000,
)
SESSION_LIST_FILTERED = LatencyBudget(
    "test_list_sessions_with_status_filter_latency",
    regression=0.008,
    product_target=0.150,
    reference=0.003005,
)
SESSION_LIST_BY_USER = LatencyBudget(
    "test_list_sessions_by_user_latency",
    regression=0.008,
    product_target=0.200,
    reference=0.003005,
)
SESSION_STATUS_UPDATE = LatencyBudget(
    "test_session_status_update_latency",
    regression=0.008,
    product_target=0.150,
    reference=0.002825,
)
SESSION_COMPLETION_UPDATE = LatencyBudget(
    "test_session_completion_update_latency",
    regression=0.006,
    product_target=0.150,
    reference=0.002705,
)
SESSION_TOKEN_USAGE_UPDATE = LatencyBudget(
    "test_session_token_usage_update_latency",
    regression=0.006,
    product_target=0.150,
    reference=0.002705,
)
SESSION_DELETE = LatencyBudget(
    "test_session_delete_latency",
    regression=0.006,
    product_target=0.150,
    reference=0.002405,
)
SESSION_COUNT = LatencyBudget(
    "test_count_sessions_latency",
    regression=0.005,
    product_target=0.050,
    reference=0.001900,
)
SESSION_LIFECYCLE_WORKLOAD = LatencyBudget(
    "test_session_lifecycle_workload",
    regression=0.030,
    product_target=0.600,
    reference=0.012605,
)
SESSION_PAUSE_RESUME_WORKLOAD = LatencyBudget(
    "test_session_pause_resume_workload",
    regression=0.050,
    product_target=0.900,
    reference=0.018325,
)

# --- tests/benchmarks/test_investigation_session_service_operations.py -------

SESSION_SERVICE_CREATE = LatencyBudget(
    "test_benchmark_create_session",
    regression=0.050,
    product_target=0.200,
    reference=0.019045,
)
SESSION_SERVICE_GET = LatencyBudget(
    "test_benchmark_get_session",
    regression=0.050,
    product_target=0.100,
    reference=0.018939,
)
SESSION_SERVICE_UPDATE = LatencyBudget(
    "test_benchmark_update_session",
    regression=0.050,
    product_target=0.150,
    reference=0.019258,
)
SESSION_SERVICE_PAUSE = LatencyBudget(
    "test_benchmark_pause_session",
    regression=0.050,
    product_target=0.150,
    reference=0.019047,
)
SESSION_SERVICE_RESUME = LatencyBudget(
    "test_benchmark_resume_session",
    regression=0.050,
    product_target=0.150,
    reference=0.019020,
)
SESSION_SERVICE_COMPLETE = LatencyBudget(
    "test_benchmark_complete_session",
    regression=0.050,
    product_target=0.150,
    reference=0.018537,
)
SESSION_SERVICE_LIST_50 = LatencyBudget(
    "test_benchmark_list_sessions_50",
    regression=0.050,
    product_target=0.300,
    reference=0.020203,
)
SESSION_SERVICE_CHECK_BUDGET = LatencyBudget(
    "test_benchmark_check_budget_exceeded",
    regression=0.050,
    product_target=0.100,
    reference=0.018609,
)
SESSION_SERVICE_STATISTICS = LatencyBudget(
    "test_benchmark_get_statistics_100_sessions",
    regression=0.050,
    product_target=0.500,
    reference=0.021672,
)

# --- tests/benchmarks/test_knowledge_item_operations.py ----------------------

ITEM_CREATE = LatencyBudget(
    "test_single_item_creation_latency",
    regression=0.012,
    product_target=0.200,
    reference=0.004515,
)
ITEM_CREATE_WITH_EMBEDDING = LatencyBudget(
    "test_item_creation_with_embedding_latency",
    regression=0.012,
    product_target=0.200,
    reference=0.004810,
)
ITEM_CREATE_WITH_METADATA = LatencyBudget(
    "test_item_creation_with_metadata_latency",
    regression=0.012,
    product_target=0.200,
    reference=0.004510,
)
#: Named "throughput" but compared as a LATENCY: the site asserts the batch
#: duration and prints the rate. Left as it is; renaming a test breaks the
#: join with 90 days of artifacts for no gain.
ITEM_BULK_CREATE = LatencyBudget(
    "test_bulk_item_creation_throughput",
    regression=1.000,
    product_target=2.000,
    reference=0.427235,
)
ITEM_RETRIEVE = LatencyBudget(
    "test_single_item_retrieval_latency",
    regression=0.005,
    product_target=0.100,
    reference=0.001900,
)
ITEM_TAG_SEARCH_ANY = LatencyBudget(
    "test_tag_search_match_any_latency",
    regression=0.060,
    product_target=0.400,
    reference=0.022805,
)
#: #908's canary. Against the 20 runs' median it sat at 5.7% of its 400 ms
#: budget; it now sits at 37.9% of a 60 ms one.
ITEM_TAG_SEARCH_ALL = LatencyBudget(
    "test_tag_search_match_all_latency",
    regression=0.060,
    product_target=0.400,
    reference=0.024300,
)
ITEM_UPDATE = LatencyBudget(
    "test_item_update_latency",
    regression=0.008,
    product_target=0.150,
    reference=0.003105,
)
ITEM_EMBEDDING_UPDATE = LatencyBudget(
    "test_item_embedding_update_latency",
    regression=0.008,
    product_target=0.150,
    reference=0.003505,
)
ITEM_WITHOUT_EMBEDDINGS = LatencyBudget(
    "test_get_items_without_embeddings_latency",
    regression=0.008,
    product_target=0.150,
    reference=0.003600,
)
ITEM_MOST_HELPFUL = LatencyBudget(
    "test_get_most_helpful_latency",
    regression=0.012,
    product_target=0.200,
    reference=0.004700,
)
ITEM_LIFECYCLE_WORKLOAD = LatencyBudget(
    "test_item_lifecycle_workload",
    regression=0.025,
    product_target=0.500,
    reference=0.010470,
)
ITEM_BROWSING_WORKLOAD = LatencyBudget(
    "test_knowledge_base_browsing_workload",
    regression=0.025,
    product_target=0.800,
    reference=0.009715,
)


#: Every budget in the table, by constant name. Built from this module's own
#: namespace so it cannot fall behind the constants above — the guard in
#: ``tests/unit/ci/test_benchmark_calibration.py`` checks it against the
#: suite, which is the direction that can actually drift.
ALL_BUDGETS: Dict[str, Budget] = {
    _name: _value
    for _name, _value in sorted(globals().items())
    if isinstance(_value, Budget)
}
