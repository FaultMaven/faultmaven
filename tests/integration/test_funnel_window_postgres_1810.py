"""The funnel quantile window (#1810) on real PostgreSQL.

``closed_at`` is ``timestamptz`` here, so the SQL prefilter compares a bound
aware datetime. The table lives in a throwaway schema, uuid-suffixed so
concurrent runs never drop each other's; the real ``cases`` table is never
touched.
"""

import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.observability import funnel_metrics
from faultmaven.infrastructure.observability.funnel_metrics import (
    QUANTILE_WINDOW,
    FunnelMetricsCollector,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

_SCHEMA = f"fm_test_1810_{uuid.uuid4().hex[:8]}"
_NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
_CUT = _NOW - QUANTILE_WINDOW

# (offset from the window start, in window?, current_turn). In-window turns are
# 1-3; out-of-window turns are huge so any leak moves p95.
_ROWS = (
    (timedelta(seconds=1), True, 1),
    (-timedelta(seconds=1), False, 9000),
    (-timedelta(hours=23), False, 9001),
    (-timedelta(hours=25), False, 9002),
    (-timedelta(days=3), False, 9003),
    (timedelta(days=3), True, 2),
    (QUANTILE_WINDOW, True, 3),
)


def _values(g):
    return {
        (c.kwargs["to_state"], c.kwargs["quantile"]): v.args[0]
        for c, v in zip(
            g.labels.call_args_list, g.labels.return_value.set.call_args_list
        )
    }


async def _refresh(maker):
    """Refresh on ``maker``; return the turns and duration gauges and the rows
    that survived the window (what both quantiles were computed from)."""

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
        patch.object(funnel_metrics, "resolution_turns_quantile_7d") as turns_g,
        patch.object(funnel_metrics, "duration_seconds_quantile_7d") as dur_g,
        patch.object(
            FunnelMetricsCollector,
            "_set_quantiles",
            autospec=True,
            side_effect=FunnelMetricsCollector._set_quantiles,
        ) as spy,
    ):
        await FunnelMetricsCollector().refresh(now=_NOW)
    return _values(turns_g), _values(dur_g), spy.call_args.args[1]


async def _with_table(rows):
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        connect_args={"server_settings": {"search_path": _SCHEMA}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
            await conn.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
            await conn.execute(
                text(
                    "CREATE TABLE cases (case_id text, state text, "
                    "closure_reason text, current_turn int, "
                    "created_at timestamptz, closed_at timestamptz)"
                )
            )
            for i, (turns, closed) in enumerate(rows):
                await conn.execute(
                    text(
                        "INSERT INTO cases VALUES (:id, 'resolved', NULL, :t, :c, :e)"
                    ),
                    {
                        "id": str(i),
                        "t": turns,
                        "c": None if closed is None else closed - timedelta(hours=1),
                        "e": closed,
                    },
                )
        maker = async_sessionmaker(engine, expire_on_commit=False)
        return await _refresh(maker)
    finally:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        finally:
            await engine.dispose()


async def test_window_keeps_exactly_the_in_window_rows():
    rows = [(turns, _CUT + off) for off, _inside, turns in _ROWS]
    rows.append((9999, None))  # NULL closed_at: in no window
    turns, durations, windowed = await _with_table(rows)

    # The exact windowed set: every in-window row, none out of window. A
    # quantile cannot show this: p50/p95 over [1, 2, 3] and [2, 3] are equal.
    assert sorted((r[2], r[4]) for r in windowed) == [
        (t, _CUT + off) for off, inside, t in _ROWS if inside
    ]
    assert turns[("resolved", "0.5")] == 2.0
    assert turns[("resolved", "0.95")] == 3.0
    assert durations[("resolved", "0.5")] == 3600.0
    assert durations[("resolved", "0.95")] == 3600.0


async def test_empty_window_publishes_nan():
    old = _NOW - timedelta(days=9)
    turns, durations, windowed = await _with_table([(5, old)])
    assert windowed == []
    assert all(math.isnan(v) for v in turns.values())
    assert all(math.isnan(v) for v in durations.values())
    assert len(turns) == 2 * len(funnel_metrics._EFFORT_TO_STATES)
