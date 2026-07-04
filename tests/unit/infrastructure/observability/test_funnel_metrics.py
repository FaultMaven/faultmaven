"""Unit tests for the case-funnel state-projection metrics collector.

The collector projects the durable ``cases`` table into Prometheus gauges, so
the tests exercise the pure projection logic (count normalization + quantiles)
and the gauge-publishing in ``refresh`` against a mocked DB session — no real
counters, no transition instrumentation.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.observability import funnel_metrics
from faultmaven.infrastructure.observability.funnel_metrics import (
    FunnelMetricsCollector,
    _percentile,
)


@pytest.mark.unit
class TestPercentile:
    def test_empty_is_zero(self):
        assert _percentile([], 0.5) == 0.0

    def test_single_value(self):
        assert _percentile([7], 0.95) == 7.0

    def test_nearest_rank(self):
        assert _percentile([1, 2, 3, 4], 0.5) == 2.0
        assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10.0

    def test_unsorted_input(self):
        assert _percentile([4, 1, 3, 2], 0.5) == 2.0


@pytest.mark.unit
class TestNormalizeCounts:
    def setup_method(self):
        self.c = FunnelMetricsCollector()

    def test_full_bounded_matrix_is_seeded_to_zero(self):
        # Empty DB -> every bounded combination present at 0 (no stale series).
        out = self.c._normalize_counts([])
        assert out[("inquiry", "none")] == 0
        assert out[("investigating", "none")] == 0
        assert out[("resolved", "none")] == 0
        assert out[("closed", "inquiry_only")] == 0
        assert out[("closed", "closed_after_investigation")] == 0
        assert out[("closed", "closed_insufficient_evidence")] == 0
        assert out[("closed", "unknown")] == 0

    def test_insufficient_evidence_close_gets_its_own_bucket(self):
        # Regression guard: the Phase-3 closure reason must be counted on its own
        # series, NOT collapsed into "unknown" (which would make data-wall closes
        # indistinguishable from the D4 integrity gap).
        out = self.c._normalize_counts([("closed", "closed_insufficient_evidence", 3)])
        assert out[("closed", "closed_insufficient_evidence")] == 3
        assert out[("closed", "unknown")] == 0

    def test_closed_reasons_and_open_states(self):
        out = self.c._normalize_counts(
            [
                ("investigating", None, 3),
                ("resolved", None, 4),
                ("closed", "inquiry_only", 2),
                ("closed", "closed_after_investigation", 1),
            ]
        )
        assert out[("investigating", "none")] == 3
        assert out[("resolved", "none")] == 4
        assert out[("closed", "inquiry_only")] == 2
        assert out[("closed", "closed_after_investigation")] == 1

    def test_closed_without_reason_maps_to_unknown(self):
        # The D4 integrity gap: a CLOSED case with no classified reason must be
        # visible, not dropped.
        out = self.c._normalize_counts([("closed", None, 5)])
        assert out[("closed", "unknown")] == 5

    def test_unrecognized_closure_reason_maps_to_unknown(self):
        out = self.c._normalize_counts([("closed", "weird_value", 2)])
        assert out[("closed", "unknown")] == 2


@pytest.mark.unit
class TestSetQuantiles:
    def test_splits_effort_states_by_closure_reason(self):
        c = FunnelMetricsCollector()
        with patch.object(funnel_metrics, "resolution_turns_quantile") as g:
            c._set_quantiles(
                [
                    ("resolved", None, 4),
                    ("resolved", None, 8),
                    ("closed", "closed_after_investigation", 2),
                    ("closed", "closed_insufficient_evidence", 6),
                ]
            )
        # Three effort to_states x two quantiles = 6 label/set calls.
        assert g.labels.call_count == 6
        labelled = {
            (kw["to_state"], kw["quantile"]) for _, kw in g.labels.call_args_list
        }
        assert ("resolved", "0.5") in labelled
        assert ("resolved", "0.95") in labelled
        assert ("closed_after_investigation", "0.5") in labelled
        # Insufficient-evidence closes are a distinct effort series, not folded
        # into closed_after_investigation.
        assert ("closed_insufficient_evidence", "0.5") in labelled
        assert ("closed_insufficient_evidence", "0.95") in labelled


@pytest.mark.unit
class TestRefresh:
    async def test_refresh_queries_db_and_publishes_gauges(self):
        c = FunnelMetricsCollector()

        # Two execute() calls: funnel counts, then effort turns.
        session = AsyncMock()
        count_result = MagicMock()
        count_result.all.return_value = [("investigating", None, 2)]
        effort_result = MagicMock()
        effort_result.all.return_value = [("resolved", None, 5)]
        session.execute.side_effect = [count_result, effort_result]

        @asynccontextmanager
        async def fake_session():
            yield session

        with (
            patch(
                "faultmaven.infrastructure.persistence.database.get_db_session",
                fake_session,
            ),
            patch.object(funnel_metrics, "cases_gauge") as cases_g,
            patch.object(funnel_metrics, "resolution_turns_quantile") as turns_g,
        ):
            await c.refresh()

        # Funnel gauge set for every bounded combo (>= 6); investigating=2 present.
        assert cases_g.labels.call_count >= 6
        # Effort quantiles published.
        assert turns_g.labels.called
        assert session.execute.await_count == 2
