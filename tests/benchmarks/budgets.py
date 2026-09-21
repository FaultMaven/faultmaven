"""The per-PR regression anchor and product target for every benchmark here.

The machinery — the two-number ``Budget`` dataclasses, the 2-3x band they
enforce at import time, and ``asserted_target``'s per-PR / nightly split —
lives in ``tests/wallclock/budgets.py``, shared with
``tests/performance/``. That module's docstring carries the #1556 ruling
and what a 2-3x anchor gives up. This module carries the table and where
its numbers came from.

Nothing here moves a product target. Every ``product_target`` below is the
number that test asserted before #1556, carried over unchanged — moving one
is a product decision #908's ruling reserved to the owner.

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
above it.
"""

from __future__ import annotations

from typing import Dict

from tests.wallclock.budgets import (
    Budget,
    LatencyBudget,
    ThroughputBudget,
    collect_budgets,
)

__all__ = ["Budget", "LatencyBudget", "ThroughputBudget", "ALL_BUDGETS"]

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


#: Every budget in this table, by constant name.
ALL_BUDGETS: Dict[str, Budget] = collect_budgets(globals())
