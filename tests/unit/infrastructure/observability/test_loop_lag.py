"""The loop-lag ticker records how late each wake-up was (#2056).

Values come from an injected clock, never from the wall clock. The one test
of the default clock asserts only a lower bound, set by an offset it adds to
the loop's own clock.
"""

import asyncio
import logging
import re
from pathlib import Path

import pytest

from faultmaven.infrastructure.observability import loop_lag

pytestmark = pytest.mark.unit

INTERVAL = 0.25


class _Recorder:
    def __init__(self, fail_first: int = 0):
        self.values: list[float] = []
        self.calls = 0
        self.fail_first = fail_first
        self.event = asyncio.Event()

    def observe(self, value: float) -> None:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("boom")
        self.values.append(value)
        self.event.set()


class _StopTicking(Exception):
    pass


async def _run_scripted(monkeypatch, extra_per_tick):
    """Run the ticker with a scripted clock; ``sleep`` advances it by
    ``interval + extra``. Returns the observations."""
    rec = _Recorder()
    monkeypatch.setattr(loop_lag, "event_loop_lag_seconds", rec)
    now = [100.0]
    extras = iter(extra_per_tick)

    async def sleep(interval):
        try:
            extra = next(extras)
        except StopIteration:
            raise _StopTicking
        now[0] += interval + extra

    with pytest.raises(_StopTicking):
        await loop_lag.monitor.run_periodic(INTERVAL, clock=lambda: now[0], sleep=sleep)
    return rec.values


async def test_on_time_late_and_early_ticks(monkeypatch):
    values = await _run_scripted(monkeypatch, [0.0, 3.0, -1.0])
    assert values == [0.0, pytest.approx(3.0), 0.0]


async def test_real_loop_hold_is_observed_and_idle_is_zero(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(loop_lag, "event_loop_lag_seconds", rec)
    now = [0.0]
    task = asyncio.create_task(
        loop_lag.monitor.run_periodic(0.001, clock=lambda: now[0])
    )
    try:
        # Control: with no hold, every observation is 0.0.
        for _ in range(3):
            rec.event.clear()
            await asyncio.wait_for(rec.event.wait(), 10)
        assert rec.values and all(v == 0.0 for v in rec.values)

        # A synchronous "hold": the loop does nothing while the clock moves 5.0.
        rec.event.clear()
        seen = len(rec.values)
        now[0] += 5.0
        await asyncio.wait_for(rec.event.wait(), 10)
        # the fake clock stands still during the sleep, so the tick's own
        # interval is subtracted: lag = hold - interval.
        assert rec.values[seen] == pytest.approx(5.0 - 0.001)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)


async def test_failing_observe_is_logged_once_and_loop_continues(monkeypatch, caplog):
    rec = _Recorder(fail_first=2)
    monkeypatch.setattr(loop_lag, "event_loop_lag_seconds", rec)

    async def sleep(_):
        if rec.values:
            raise _StopTicking
        await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, logger=loop_lag.logger.name):
        with pytest.raises(_StopTicking):
            await loop_lag.monitor.run_periodic(
                INTERVAL, clock=lambda: 0.0, sleep=sleep
            )
    assert rec.calls == 3 and rec.values == [0.0]
    assert [
        r for r in caplog.records if "RuntimeError" in r.getMessage()
    ].__len__() == 1


async def test_cancel_ends_the_task_with_cancelled_error(monkeypatch):
    monkeypatch.setattr(loop_lag, "event_loop_lag_seconds", _Recorder())
    task = asyncio.create_task(loop_lag.monitor.run_periodic())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert task.cancelled()


class _OffsetLoop(asyncio.SelectorEventLoop):
    """A real loop whose ``time()`` runs ``offset`` seconds ahead."""

    offset = 0.0

    def time(self) -> float:
        return super().time() + self.offset


def test_the_default_clock_is_the_running_loops_time(monkeypatch):
    """With no ``clock=``, the ticker reads the running loop's ``time``.

    The loop's clock jumps 5.0 ahead between two ticks, so the tick in flight
    reads at least ``5.0 - LOOP_LAG_TICK_SECONDS`` late. A lower bound only:
    real elapsed time can only add to it. A ticker that read any other clock
    (a constant, ``time.time``) misses the jump.
    """
    rec = _Recorder()
    monkeypatch.setattr(loop_lag, "event_loop_lag_seconds", rec)
    loop = _OffsetLoop()

    async def scenario():
        task = asyncio.create_task(loop_lag.monitor.run_periodic())
        try:
            await asyncio.wait_for(rec.event.wait(), 10)
            # The ticker computed this tick's deadline before yielding, so the
            # jump lands inside the tick in flight, the next one observed.
            rec.event.clear()
            seen = len(rec.values)
            loop.offset += 5.0
            await asyncio.wait_for(rec.event.wait(), 10)
            return rec.values[seen]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)

    try:
        lag = loop.run_until_complete(scenario())
    finally:
        loop.close()
    assert lag >= 5.0 - loop_lag.LOOP_LAG_TICK_SECONDS


#: The monitoring README, whose PromQL reads this histogram's buckets.
_README = Path(__file__).resolve().parents[4] / "docs/operations/monitoring/README.md"
#: Every selector on the histogram's buckets, and every ``le`` matcher in one.
_BUCKET_SELECTOR = re.compile(r"faultmaven_event_loop_lag_seconds_bucket\{([^}]*)\}")
_LE_MATCHER = re.compile(r'\ble\s*=\s*"([^"]*)"')


def test_the_documented_bucket_selectors_match_the_exposition():
    """Each ``le`` the README selects is a label the exporter renders.

    ``prometheus_client`` renders the bound 10 as ``le="10.0"``, so a query on
    ``le="10"`` returns no data at all rather than a wrong number.
    """
    prometheus_client = pytest.importorskip("prometheus_client")
    from prometheus_client.parser import text_string_to_metric_families

    registry = prometheus_client.CollectorRegistry()
    histogram = prometheus_client.Histogram(
        "faultmaven_event_loop_lag_seconds",
        "loop lag",
        buckets=list(loop_lag.LOOP_LAG_BUCKETS),
        registry=registry,
    )
    histogram.observe(12.0)
    exposition = prometheus_client.generate_latest(registry).decode()
    rendered = {
        sample.labels["le"]
        for family in text_string_to_metric_families(exposition)
        for sample in family.samples
        if sample.name == "faultmaven_event_loop_lag_seconds_bucket"
    }

    selected = [
        label
        for selector in _BUCKET_SELECTOR.findall(_README.read_text())
        for label in _LE_MATCHER.findall(selector)
    ]
    assert selected, f"{_README} selects no bucket of this histogram"
    missing = sorted(set(selected) - rendered)
    assert not missing, f"selected {missing}, rendered {sorted(rendered)}"
