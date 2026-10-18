"""The loop-lag ticker records how late each wake-up was (#2056).

Values come from an injected clock, never from the wall clock.
"""

import asyncio
import logging

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
            await task


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
        await task
    assert task.cancelled()
