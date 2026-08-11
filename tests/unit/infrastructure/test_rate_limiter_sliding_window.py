"""The sliding window counts requests, not wall-clock seconds (fm#920).

The window is a Redis sorted set holding one element per request inside it.
These tests use ``1 < requests < window`` and drive more calls than the limit
at a single frozen instant — the only shape that separates counting requests
from counting distinct seconds, and the shape the pre-fix suite lacked.

See docs/architecture/security/rate-limiting-sliding-window.md.
"""

import time as std_time

import fakeredis.aioredis as fakeredis_aio
import pytest

import faultmaven.infrastructure.protection.rate_limiter as rate_limiter_module
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter
from faultmaven.models.protection import LimitType, RateLimitConfig, RateLimitSpec

pytestmark = pytest.mark.unit


class _Clock:
    """A controllable stand-in for the module's ``time`` reference.

    Only ``time()`` is controlled. ``monotonic()`` passes through to the real
    clock because the limiter uses it for liveness bookkeeping (log throttling,
    demotion), which has nothing to do with the window and must not be frozen.
    """

    def __init__(self, now: float):
        self._now = now

    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return std_time.monotonic()

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def redis_client():
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _limiter(client, *, requests: int, window: int = 60) -> RedisRateLimiter:
    """A limiter enforcing one ``global`` limit against ``client``."""
    limiter = RedisRateLimiter()
    await limiter._adopt(client, owns=False, degraded=False)
    limiter.configure_limits(
        {
            LimitType.GLOBAL.value: RateLimitConfig(
                enabled=True, requests=requests, window=window
            )
        }
    )
    return limiter


def _window_key(limiter: RedisRateLimiter, key: str) -> str:
    return f"{limiter.key_prefix}:{LimitType.GLOBAL.value}:{key}"


async def _verdicts(limiter, key, count):
    """Drive ``count`` checks back to back; return the allow/block sequence.

    Asserts each verdict came from a real check: ``check_rate_limits`` reports
    ``limit=0`` when it swallowed a Redis error and failed open, which would
    otherwise read as a legitimate "allowed".
    """
    out = []
    for _ in range(count):
        (result,) = await limiter.check_rate_limits(
            [RateLimitSpec(key=key, limit_type=LimitType.GLOBAL)]
        )
        assert (
            result.limit == limiter._configs[LimitType.GLOBAL.value].requests
        ), "the check failed open on a Redis error instead of deciding"
        out.append(result.allowed)
    return out


@pytest.mark.parametrize("limit", [2, 5, 17])
async def test_the_window_counts_requests_not_seconds(redis_client, monkeypatch, limit):
    """The property, swept: N back-to-back requests against L allow exactly L.

    The clock is frozen, so every call in the sweep arrives at literally the same
    instant — the strongest form of the property, and one a slow machine cannot
    weaken by letting the sweep straddle a second boundary. Under per-second
    counting only the first request of an instant is counted, so the allowed run
    collapses to 1 and ``ZCARD`` never leaves 1.
    """
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(rate_limiter_module, "time", clock)

    limiter = await _limiter(redis_client, requests=limit, window=60)
    key = f"10.1.0.{limit}"
    overshoot = 7

    verdicts = await _verdicts(limiter, key, limit + overshoot)

    assert verdicts == [True] * limit + [False] * overshoot, verdicts
    assert await redis_client.zcard(_window_key(limiter, key)) == limit


async def test_two_requests_in_the_same_second_are_two_entries(redis_client):
    """The direct anti-regression, on the real clock: distinct members per request.

    ZADD with the whole-second timestamp as member updated a score rather than
    adding an element, so the set stayed at one entry.
    """
    limiter = await _limiter(redis_client, requests=2, window=60)
    key = "10.1.1.1"

    assert await _verdicts(limiter, key, 2) == [True, True]
    assert await redis_client.zcard(_window_key(limiter, key)) == 2


async def test_the_window_slides_as_entries_age_out(redis_client, monkeypatch):
    """Quota consumed at t₀ is released once t₀ falls out of the window."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(rate_limiter_module, "time", clock)

    limiter = await _limiter(redis_client, requests=3, window=10)
    key = "10.1.2.1"

    assert await _verdicts(limiter, key, 4) == [True, True, True, False]
    assert await redis_client.zcard(_window_key(limiter, key)) == 3

    # Past t₀ + window: the three entries are outside the window and must be
    # pruned, not merely ignored — the set is the only record of the count.
    clock.advance(11)

    assert await _verdicts(limiter, key, 1) == [True]
    assert await redis_client.zcard(_window_key(limiter, key)) == 1


async def test_blocked_requests_consume_no_quota(redis_client, monkeypatch):
    """A refused request must not insert: it neither counts nor extends the window.

    Inserting on the blocked path would let a client that keeps hammering a
    limit hold its own quota shut indefinitely.
    """
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(rate_limiter_module, "time", clock)

    limiter = await _limiter(redis_client, requests=2, window=10)
    key = "10.1.3.1"
    window_key = _window_key(limiter, key)

    assert await _verdicts(limiter, key, 2) == [True, True]
    assert await redis_client.zcard(window_key) == 2

    # Refused requests, four seconds after the allowed pair.
    clock.advance(4)
    assert await _verdicts(limiter, key, 5) == [False] * 5
    assert await redis_client.zcard(window_key) == 2, "a blocked request inserted"

    # The window frees when the *allowed* entries age out (t₀ + 10), not when
    # the blocked ones would have (t₀ + 14).
    clock.advance(7)

    assert await _verdicts(limiter, key, 1) == [True]
    assert await redis_client.zcard(window_key) == 1


async def test_an_entry_exactly_one_window_old_is_outside_the_window(
    redis_client, monkeypatch
):
    """The bound is inclusive: an entry scored exactly ``window`` ago is pruned.

    This pins which side of the edge the bound falls on, so the window is the
    half-open interval (now − window, now] and not the closed one. It is the
    boundary case the sliding test steps over (it advances 11 against a 10s
    window) and the one where an off-by-one in either path — enforcement's prune
    or status's count — leaves quota held for one extra tick.

    Probed through enforcement rather than a status read: the entry sitting
    exactly on the bound must be pruned by the very next check, which is
    observable as that check being admitted against a limit of one and the set
    still holding a single element afterwards. A second admitted request against
    ``requests=1`` is only possible if the first was released.
    """
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(rate_limiter_module, "time", clock)

    limiter = await _limiter(redis_client, requests=1, window=10)
    key = "10.1.5.1"
    window_key = _window_key(limiter, key)

    assert await _verdicts(limiter, key, 1) == [True]
    assert await redis_client.zcard(window_key) == 1

    # Exactly on the bound, not past it.
    clock.advance(10)

    # The entry on the bound is released, so the quota it held is free.
    assert await _verdicts(limiter, key, 1) == [
        True
    ], "an entry sitting exactly on the bound was still counted"
    # The aged-out entry was pruned rather than left alongside the new one.
    assert await redis_client.zcard(window_key) == 1
