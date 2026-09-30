"""Unit tests for the case-funnel state-projection metrics collector.

The collector projects the durable ``cases`` table into Prometheus gauges, so
the tests exercise the pure projection logic (count normalization + quantiles)
and the gauge-publishing in ``refresh`` against a mocked DB session — no real
counters, no transition instrumentation.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.observability import funnel_metrics
from faultmaven.infrastructure.observability.funnel_metrics import (
    FunnelMetricsCollector,
    _as_utc,
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
        assert out[("closed", "mitigation_sufficient")] == 0
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
                ("closed", "mitigation_sufficient", 1),
            ]
        )
        assert out[("investigating", "none")] == 3
        assert out[("resolved", "none")] == 4
        assert out[("closed", "inquiry_only")] == 2
        assert out[("closed", "mitigation_sufficient")] == 1

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
                    ("resolved", None, 4, None, None),
                    ("resolved", None, 8, None, None),
                    ("closed", "mitigation_sufficient", 2, None, None),
                    ("closed", "closed_insufficient_evidence", 6, None, None),
                ]
            )
        # Every effort to_state x two quantiles. The full matrix is seeded
        # regardless of which reasons appear in the rows, so vanished series
        # reset to zero rather than retaining a stale gauge value.
        assert g.labels.call_count == 2 * len(funnel_metrics._EFFORT_TO_STATES)
        labelled = {
            (kw["to_state"], kw["quantile"]) for _, kw in g.labels.call_args_list
        }
        assert ("resolved", "0.5") in labelled
        assert ("resolved", "0.95") in labelled
        assert ("mitigation_sufficient", "0.5") in labelled
        # Each closure reason is its own effort series — the whole point of
        # retiring the generic bucket was that they measure different things.
        assert ("closed_insufficient_evidence", "0.5") in labelled
        assert ("closed_insufficient_evidence", "0.95") in labelled
        assert ("solution_deferred", "0.5") in labelled
        assert ("closed_rca_infeasible", "0.5") in labelled
        # inquiry_only is the one exclusion: no investigation ran, so there is
        # no diagnostic effort to measure.
        assert not any(to_state == "inquiry_only" for to_state, _ in labelled)


@pytest.mark.unit
class TestRefresh:
    async def test_refresh_queries_db_and_publishes_gauges(self):
        c = FunnelMetricsCollector()

        # Two execute() calls: funnel counts, then effort turns.
        session = AsyncMock()
        count_result = MagicMock()
        count_result.all.return_value = [("investigating", None, 2)]
        effort_result = MagicMock()
        effort_result.all.return_value = [
            ("resolved", None, 5, datetime(2026, 1, 1), datetime(2026, 1, 1, 1))
        ]
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
            patch.object(funnel_metrics, "duration_seconds_quantile") as dur_g,
        ):
            await c.refresh()

        # Funnel gauge set for every bounded combo (>= 6); investigating=2 present.
        assert cases_g.labels.call_count >= 6
        # Effort quantiles published.
        assert turns_g.labels.called
        assert dur_g.labels.called
        assert session.execute.await_count == 2


_T0 = datetime(2026, 9, 30, 0, 0, 0, tzinfo=UTC)


def _published(rows):
    """Run ``_set_durations`` and return {(to_state, quantile): value}."""
    out: dict = {}
    with patch.object(funnel_metrics, "duration_seconds_quantile") as g:
        FunnelMetricsCollector()._set_durations(rows)
    for (_, kw), setter in zip(
        g.labels.call_args_list,
        [c.args[0] for c in g.labels.return_value.set.call_args_list],
    ):
        out[(kw["to_state"], kw["quantile"])] = setter
    return out


@pytest.mark.unit
class TestAsUtc:
    def test_datetime_passthrough_and_naive_is_utc(self):
        aware = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))
        assert _as_utc(aware) is aware
        assert _as_utc(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)

    def test_sqlite_string_shapes(self):
        assert _as_utc("2026-09-30 00:00:00+00:00") == _T0
        assert _as_utc("2026-09-30 00:00:00") == _T0
        assert _as_utc("2026-09-29 17:00:00-07:00") == _T0

    @pytest.mark.parametrize("bad", [None, "not-a-date", "", 5])
    def test_unusable_is_none(self, bad):
        assert _as_utc(bad) is None


@pytest.mark.unit
class TestSetDurations:
    def test_datetime_rows_per_to_state(self):
        rows = [
            ("resolved", None, 1, _T0, _T0 + timedelta(seconds=100)),
            ("resolved", None, 1, _T0, _T0 + timedelta(seconds=300)),
            ("resolved", None, 1, _T0, _T0 + timedelta(seconds=200)),
            (
                "closed",
                "closed_insufficient_evidence",
                1,
                _T0,
                _T0 + timedelta(hours=1),
            ),
        ]
        out = _published(rows)
        assert out[("resolved", "0.5")] == 200.0
        assert out[("resolved", "0.95")] == 300.0
        assert out[("closed_insufficient_evidence", "0.5")] == 3600.0
        assert out[("closed_insufficient_evidence", "0.95")] == 3600.0
        # Empty bucket reads 0.0, and the full matrix is published.
        assert out[("solution_deferred", "0.5")] == 0.0
        assert len(out) == 2 * len(funnel_metrics._EFFORT_TO_STATES)

    def test_sqlite_string_rows_with_offset_naive_and_mixed_offsets(self):
        rows = [
            # offset-aware, microseconds
            (
                "resolved",
                None,
                1,
                "2026-09-30 00:00:00+00:00",
                "2026-09-30 01:30:00.000005+00:00",
            ),
            # naive
            (
                "closed",
                "mitigation_sufficient",
                1,
                "2026-09-30 00:00:00",
                "2026-09-30 02:00:00",
            ),
            # mixed offsets
            (
                "closed",
                "closed_rca_infeasible",
                1,
                "2026-09-29 17:00:00-07:00",
                "2026-09-30 01:00:00+00:00",
            ),
        ]
        out = _published(rows)
        assert out[("resolved", "0.5")] == pytest.approx(5400.000005)
        assert out[("mitigation_sufficient", "0.5")] == 7200.0
        assert out[("closed_rca_infeasible", "0.5")] == 3600.0

    def test_naive_against_aware_does_not_raise(self):
        rows = [
            ("resolved", None, 1, datetime(2026, 9, 30), _T0 + timedelta(seconds=60))
        ]
        assert _published(rows)[("resolved", "0.5")] == 60.0

    def test_unusable_rows_contribute_nothing(self):
        rows = [
            ("resolved", None, 1, _T0, None),  # no closed_at
            ("resolved", None, 1, None, _T0),  # no created_at
            ("resolved", None, 1, _T0, _T0 - timedelta(seconds=5)),  # backwards
            ("resolved", None, 1, "garbage", "2026-09-30 00:00:00"),  # unparseable
            ("resolved", None, 1, _T0, _T0 + timedelta(seconds=42)),  # the only sample
        ]
        out = _published(rows)
        assert out[("resolved", "0.5")] == 42.0
        assert out[("resolved", "0.95")] == 42.0

    def test_all_unusable_reads_zero(self):
        rows = [("resolved", None, 1, _T0, _T0 - timedelta(seconds=5))]
        assert _published(rows)[("resolved", "0.5")] == 0.0

    def test_inquiry_only_row_is_ignored_if_it_arrives(self):
        rows = [("closed", "inquiry_only", 1, _T0, _T0 + timedelta(seconds=99))]
        out = _published(rows)
        assert not any(to_state == "inquiry_only" for to_state, _ in out)
        assert all(v == 0.0 for v in out.values())


@pytest.mark.unit
class TestRefreshAgainstRealSqlite:
    """The collector path on a real SQLite engine: a raw ``text()`` select
    returns timestamps as strings there, which mocked rows never exercise."""

    async def test_durations_published_from_sqlite_rows(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE cases (case_id TEXT, state TEXT, "
                    "closure_reason TEXT, current_turn INTEGER, "
                    "created_at DATETIME, closed_at DATETIME)"
                )
            )
            ins = text("INSERT INTO cases VALUES (:id, :st, :cr, :t, :c, :e)")
            rows = [
                # aware, naive and mixed-offset writes, as the repository does
                ("a", "resolved", None, 3, _T0, _T0 + timedelta(minutes=90)),
                (
                    "b",
                    "resolved",
                    None,
                    3,
                    _T0.replace(tzinfo=None),
                    (_T0 + timedelta(hours=2)).replace(tzinfo=None),
                ),
                (
                    "c",
                    "resolved",
                    None,
                    3,
                    _T0.astimezone(timezone(timedelta(hours=-7))),
                    _T0 + timedelta(hours=1),
                ),
                (
                    "d",
                    "closed",
                    "closed_insufficient_evidence",
                    2,
                    _T0,
                    _T0 + timedelta(seconds=30),
                ),
                # outside the population: never selected
                ("e", "closed", "inquiry_only", 0, _T0, _T0 + timedelta(seconds=7)),
                ("f", "investigating", None, 1, _T0, None),
            ]
            for id_, st, cr, t, c, e in rows:
                await conn.execute(
                    ins, {"id": id_, "st": st, "cr": cr, "t": t, "c": c, "e": e}
                )

        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def real_session():
            async with maker() as session:
                yield session

        with (
            patch(
                "faultmaven.infrastructure.persistence.database.get_db_session",
                real_session,
            ),
            patch.object(funnel_metrics, "cases_gauge"),
            patch.object(funnel_metrics, "resolution_turns_quantile"),
            patch.object(funnel_metrics, "duration_seconds_quantile") as g,
        ):
            await FunnelMetricsCollector().refresh()
        await engine.dispose()

        published = {
            (c.kwargs["to_state"], c.kwargs["quantile"]): v.args[0]
            for c, v in zip(
                g.labels.call_args_list, g.labels.return_value.set.call_args_list
            )
        }
        # 3600, 5400, 7200 -> p50 5400, p95 7200
        assert published[("resolved", "0.5")] == 5400.0
        assert published[("resolved", "0.95")] == 7200.0
        assert published[("closed_insufficient_evidence", "0.5")] == 30.0
        assert not any(to_state == "inquiry_only" for to_state, _ in published)
