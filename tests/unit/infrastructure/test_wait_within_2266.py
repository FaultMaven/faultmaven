"""``wait_within`` holds its deadline whatever the work does with its cancellation (#2266).

``asyncio.wait_for`` cancels the work at its timeout and then waits for the
cancellation to finish. An asyncpg query cancelled against a PostgreSQL that has
stopped answering never finishes it: the driver's CancelRequest has no timeout,
and the connection's rollback and close await it. The stand-in below is that
work: its cleanup waits on an event only the test sets.
"""

from __future__ import annotations

import asyncio

import pytest

from faultmaven.infrastructure.concurrency import bounded
from faultmaven.infrastructure.concurrency.bounded import wait_within

pytestmark = pytest.mark.unit


def _work_whose_cancellation_hangs(release: asyncio.Event, ended: list):
    async def _work():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
            ended.append("cancelled")
            raise

    return _work()


async def _settle(release: asyncio.Event) -> None:
    """Let the work given up on finish, so the test leaves nothing pending."""
    release.set()
    pending = set(bounded._abandoned)
    if pending:
        await asyncio.wait(pending, timeout=5)
    assert not bounded._abandoned


async def test_the_deadline_holds_when_the_cancellation_does_not_finish():
    release, ended = asyncio.Event(), []
    try:
        with pytest.raises(TimeoutError, match="the probe did not finish"):
            await wait_within(
                _work_whose_cancellation_hangs(release, ended), 0.05, what="the probe"
            )
        assert ended == [], "the wait outlasted the bound for the cancellation"
    finally:
        await _settle(release)
    assert ended == ["cancelled"], "the work given up on was not cancelled"


async def test_wait_for_does_not_hold_that_deadline():
    """Positive control: the stand-in is the case ``wait_for`` cannot bound."""
    release, ended = asyncio.Event(), []
    waiting = asyncio.ensure_future(
        asyncio.wait_for(_work_whose_cancellation_hangs(release, ended), 0.05)
    )
    try:
        done, _ = await asyncio.wait({waiting}, timeout=0.5)
        assert not done, "wait_for returned before the cancellation finished"
    finally:
        release.set()
        with pytest.raises(TimeoutError):
            await waiting


async def test_the_result_is_returned_within_the_deadline():
    async def _answer():
        return 42

    assert await wait_within(_answer(), 1.0, what="the probe") == 42


async def test_the_works_own_exception_propagates():
    async def _fails():
        raise ValueError("the read failed")

    with pytest.raises(ValueError, match="the read failed"):
        await wait_within(_fails(), 1.0, what="the probe")


async def test_a_cancelled_caller_propagates_at_once_and_cancels_the_work():
    release, ended = asyncio.Event(), []
    caller = asyncio.ensure_future(
        wait_within(_work_whose_cancellation_hangs(release, ended), 60, what="probe")
    )
    await asyncio.sleep(0.01)
    caller.cancel()
    try:
        done, _ = await asyncio.wait({caller}, timeout=1.0)
        assert done, "the caller's cancellation waited for the work's"
        assert caller.cancelled()
    finally:
        await _settle(release)
    assert ended == ["cancelled"]


async def test_an_exception_after_the_deadline_is_retrieved(caplog):
    """The work given up on ends later with an error: it is logged at debug,
    never reported as an exception nothing retrieved."""
    release = asyncio.Event()

    async def _fails_on_cancel():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
            raise ConnectionError("the server went away")

    with pytest.raises(TimeoutError):
        await wait_within(_fails_on_cancel(), 0.05, what="the probe")
    with caplog.at_level("DEBUG", logger="faultmaven.infrastructure.concurrency"):
        await _settle(release)
    assert "the probe, given up on, ended with ConnectionError" in (caplog.text)
