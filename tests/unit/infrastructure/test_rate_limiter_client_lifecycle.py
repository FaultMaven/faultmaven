"""Adopting a new client must not leak the one it replaces (fm#921).

``_adopt`` overwrote ``self._redis`` and nothing ever closed the outgoing
client. On a flapping Redis the degrade ladder is re-entered on every death, so
each cycle stranded one factory-built connection pool (``max_connections`` 20)
for the pod's remaining life.

The second half of the same issue: ``check_rate_limit`` caught ``Exception``,
and ``asyncio.CancelledError`` has been a ``BaseException`` since 3.8. A check
stalled against a dead pool and cancelled by an outer per-request timeout never
moved the failure counter, so a pool that only ever stalls could not reach the
demotion threshold and the ladder was never re-entered.
"""

import asyncio

import pytest

from faultmaven.infrastructure.protection.rate_limiter import (
    CHECK_FAILURE_DEMOTION_THRESHOLD,
    RedisRateLimiter,
)
from faultmaven.models.protection import LimitType, RateLimitConfig

pytestmark = pytest.mark.unit


class _RecordingClient:
    """A stand-in real client that records whether it was closed."""

    def __init__(self, name="client"):
        self.name = name
        self.closes = 0

    async def ping(self):
        return True

    def register_script(self, script):
        async def _run(keys=None, args=None):
            raise AssertionError("this stand-in is never checked against")

        return _run

    async def close(self):
        self.closes += 1


class _StallingClient(_RecordingClient):
    """A client whose check never returns — the shape an outer timeout cancels."""

    def register_script(self, script):
        async def _run(keys=None, args=None):
            await asyncio.Event().wait()

        return _run


def _configured(limiter):
    limiter.configure_limits(
        {LimitType.GLOBAL.value: RateLimitConfig(enabled=True, requests=5, window=60)}
    )
    return limiter


async def test_re_adoption_closes_the_pool_it_replaces():
    """The leak itself: one stranded pool per demotion cycle."""
    limiter = RedisRateLimiter()
    first = _RecordingClient("first")
    second = _RecordingClient("second")

    await limiter._adopt(first, owns=True, degraded=False)
    await limiter._adopt(second, owns=True, degraded=False)

    assert first.closes == 1
    assert second.closes == 0
    assert limiter._redis is second


async def test_re_adopting_the_same_object_does_not_close_it():
    """Recovery can hand back the identical client; closing it would kill it."""
    limiter = RedisRateLimiter()
    client = _RecordingClient()

    await limiter._adopt(client, owns=True, degraded=False)
    await limiter._adopt(client, owns=True, degraded=False)

    assert client.closes == 0
    assert limiter._redis is client


async def test_a_client_the_limiter_does_not_own_is_never_closed():
    """The shared client backs sessions, revocation, dedup and idempotency."""
    limiter = RedisRateLimiter()
    shared = _RecordingClient("shared")

    await limiter._adopt(shared, owns=False, degraded=False)
    await limiter._adopt(_RecordingClient("replacement"), owns=True, degraded=False)

    assert shared.closes == 0


async def test_the_stand_in_is_never_closed(monkeypatch):
    """The FakeRedis singleton is process-wide; closing it breaks everything.

    Belt and braces over ``_owns_client``, which should already exclude it —
    asserted here because the consequence of the flag ever being wrong is that
    one subsystem's re-adoption takes down every other one.
    """
    import faultmaven.infrastructure.redis_client as rc

    stand_in = _RecordingClient("stand-in")
    monkeypatch.setattr(rc, "is_fakeredis", lambda client: client is stand_in)

    limiter = RedisRateLimiter()
    # Deliberately mislabelled as owned: the identity test must still refuse.
    await limiter._adopt(stand_in, owns=True, degraded=True)
    await limiter._adopt(_RecordingClient("real"), owns=True, degraded=False)

    assert stand_in.closes == 0


async def test_a_failure_to_close_does_not_break_the_adoption():
    """The pool being replaced is usually the one that just died."""

    class _UncloseableClient(_RecordingClient):
        async def close(self):
            raise ConnectionError("already gone")

    limiter = RedisRateLimiter()
    replacement = _RecordingClient("replacement")

    await limiter._adopt(_UncloseableClient(), owns=True, degraded=False)
    await limiter._adopt(replacement, owns=True, degraded=False)

    assert limiter._redis is replacement
    assert limiter._owns_client is True


async def test_a_cancelled_check_propagates_and_counts_as_a_failure():
    """``CancelledError`` is a ``BaseException``: ``except Exception`` misses it.

    Both halves matter — the cancellation must reach the caller (swallowing it
    would break structured cancellation) and it must move the failure run, or a
    stalling pool never demotes.
    """
    limiter = _configured(RedisRateLimiter())
    await limiter._adopt(_StallingClient(), owns=False, degraded=False)

    check = asyncio.ensure_future(
        limiter.check_rate_limit("10.5.0.1", LimitType.GLOBAL)
    )
    await asyncio.sleep(0)
    check.cancel()

    with pytest.raises(asyncio.CancelledError):
        await check

    assert limiter._consecutive_check_failures == 1


async def test_a_run_of_cancelled_checks_demotes_the_client():
    """The consequence the counter exists for: the ladder is re-entered."""
    limiter = _configured(RedisRateLimiter())
    await limiter._adopt(_StallingClient(), owns=False, degraded=False)
    before = limiter.demotion_generation

    for _ in range(CHECK_FAILURE_DEMOTION_THRESHOLD):
        check = asyncio.ensure_future(
            limiter.check_rate_limit("10.5.0.2", LimitType.GLOBAL)
        )
        await asyncio.sleep(0)
        check.cancel()
        with pytest.raises(asyncio.CancelledError):
            await check

    assert limiter.demotion_generation == before + 1


async def test_a_successful_check_still_clears_a_cancellation_run():
    """Consecutive, not cumulative — an aborting client cannot slowly demote."""
    import fakeredis.aioredis as fakeredis_aio

    limiter = _configured(RedisRateLimiter())
    await limiter._adopt(_StallingClient(), owns=False, degraded=False)

    check = asyncio.ensure_future(
        limiter.check_rate_limit("10.5.0.3", LimitType.GLOBAL)
    )
    await asyncio.sleep(0)
    check.cancel()
    with pytest.raises(asyncio.CancelledError):
        await check
    assert limiter._consecutive_check_failures == 1

    # A working client, adopted in place of the stalling one.
    await limiter._adopt(
        fakeredis_aio.FakeRedis(decode_responses=True), owns=False, degraded=False
    )
    result = await limiter.check_rate_limit("10.5.0.3", LimitType.GLOBAL)

    assert result.allowed
    assert limiter._consecutive_check_failures == 0
