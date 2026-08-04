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


class _ObservingClient(_RecordingClient):
    """Records what the limiter looked like at the moment it was closed."""

    def __init__(self, limiter, name="outgoing"):
        super().__init__(name)
        self._limiter = limiter
        self.observed_redis = "not closed"
        self.observed_script = "not closed"

    async def close(self):
        self.observed_redis = self._limiter._redis
        self.observed_script = self._limiter._window_script
        await super().close()


async def test_the_replacement_is_installed_before_the_outgoing_pool_is_closed():
    """Order, asserted from inside the close itself.

    ``await outgoing.close()`` is a yield point. With the close first, everything
    that runs during it — including a concurrent ``check_rate_limit`` snapshotting
    ``_window_script`` — sees the limiter still pointing at the client being torn
    down. Observing the limiter's state from within ``close`` is the only way to
    pin the ordering directly rather than infer it from an outcome.
    """
    limiter = RedisRateLimiter()
    outgoing = _ObservingClient(limiter)
    replacement = _RecordingClient("replacement")

    await limiter._adopt(outgoing, owns=True, degraded=False)
    outgoing_script = limiter._window_script
    await limiter._adopt(replacement, owns=True, degraded=False)

    assert outgoing.closes == 1, "the outgoing pool was not closed at all"
    assert outgoing.observed_redis is replacement, "closed before installing"
    assert (
        outgoing.observed_script is not outgoing_script
    ), "the script still pointed at the client being closed"


async def test_a_check_racing_the_teardown_lands_on_the_replacement():
    """The consequence the ordering exists to prevent.

    A check that interleaves with a slow teardown must be issued against the
    installed client, not the closing one. Driven through the real check path so
    the assertion is about where the command went, not about a flag.
    """
    import fakeredis.aioredis as fakeredis_aio

    limiter = _configured(RedisRateLimiter())
    landed = []

    class _SlowClosingClient(_RecordingClient):
        async def close(self):
            # Yield control while the teardown is in progress.
            await asyncio.sleep(0)
            result = await limiter.check_rate_limit("10.5.0.9", LimitType.GLOBAL)
            landed.append(result)
            await super().close()

    await limiter._adopt(_SlowClosingClient("outgoing"), owns=True, degraded=False)
    await limiter._adopt(
        fakeredis_aio.FakeRedis(decode_responses=True), owns=True, degraded=False
    )

    # The outgoing stand-in's script raises on use, so a check that reached it
    # would have failed open with limit 0 rather than deciding.
    assert landed, "the racing check never ran; the test is vacuous"
    assert landed[0].allowed
    assert landed[0].limit == 5, "the check was issued against the closing client"


async def test_a_stalling_teardown_does_not_stall_the_install():
    """Closing a dead pool can block; adoption must not wait behind it.

    The replacement is what ends the outage. A teardown that hangs against a
    dead socket used to hold the install behind it for as long as it took.
    """
    limiter = RedisRateLimiter()
    released = asyncio.Event()
    replacement = _RecordingClient("replacement")

    class _HangingClient(_RecordingClient):
        async def close(self):
            await released.wait()
            await super().close()

    await limiter._adopt(_HangingClient("outgoing"), owns=True, degraded=False)
    adoption = asyncio.ensure_future(
        limiter._adopt(replacement, owns=True, degraded=False)
    )
    await asyncio.sleep(0)

    try:
        assert limiter._redis is replacement, "the install waited on the teardown"
        assert limiter._owns_client is True
    finally:
        released.set()
        await adoption


async def test_the_ownership_test_reads_the_outgoing_client_s_own_flag():
    """Not its successor's.

    ``_owns_client`` is overwritten by the install, so a post-install read would
    decide the outgoing client's fate from the incoming one's provenance — and
    close the composition root's shared client the moment an owned one replaced
    it.
    """
    limiter = RedisRateLimiter()
    shared = _RecordingClient("shared")

    await limiter._adopt(shared, owns=False, degraded=False)
    await limiter._adopt(_RecordingClient("owned"), owns=True, degraded=False)

    assert shared.closes == 0, "closed a client this limiter never owned"


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
