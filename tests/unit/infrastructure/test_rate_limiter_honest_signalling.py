"""What a limited client is told must be true (fm#931).

``reset_time`` was ``now + window`` on every path and ``Retry-After`` was a
whole window plus random jitter, capped at 300s. All three are wrong for the
ordinary case: a client refused one second before its window frees was told to
wait sixty, and an hourly limit's genuine wait was silently truncated to five
minutes so the whole herd came back to be refused again.

The window itself already holds the answer — its oldest entry ages out exactly
one window after it arrived — so the script now returns that entry's score and
both paths derive from it.
"""

import time as std_time

import fakeredis.aioredis as fakeredis_aio
import pytest

import faultmaven.infrastructure.protection.rate_limiter as rate_limiter_module
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter
from faultmaven.models.protection import LimitType, RateLimitConfig

pytestmark = pytest.mark.unit

T0 = 1_700_000_000.0


class _Clock:
    """Controls only ``time()``; ``monotonic()`` stays real.

    The limiter uses the monotonic clock for liveness bookkeeping, which has
    nothing to do with the window and must not be frozen along with it.
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
def clock(monkeypatch):
    c = _Clock(T0)
    monkeypatch.setattr(rate_limiter_module, "time", c)
    return c


@pytest.fixture
def redis_client():
    return fakeredis_aio.FakeRedis(decode_responses=True)


async def _limiter(client, *, requests: int, window: int) -> RedisRateLimiter:
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


async def _check(limiter, key):
    result = await limiter.check_rate_limit(key, LimitType.GLOBAL)
    assert result.limit, "the check failed open on a Redis error instead of deciding"
    return result


async def test_a_client_refused_near_the_window_edge_waits_seconds_not_a_window(
    clock, redis_client
):
    """The defect itself: the wait is until quota frees, not a full window."""
    limiter = await _limiter(redis_client, requests=2, window=60)
    key = "10.2.0.1"

    assert (await _check(limiter, key)).allowed
    assert (await _check(limiter, key)).allowed

    # 59s later the oldest entry is one second from ageing out.
    clock.advance(59)
    refused = await _check(limiter, key)

    assert refused.allowed is False
    assert refused.retry_after == 1, refused.retry_after


async def test_the_wait_never_rounds_down_to_zero(clock, redis_client):
    """A sub-second answer reads as "retry immediately", which is not a wait."""
    limiter = await _limiter(redis_client, requests=1, window=60)
    key = "10.2.0.2"

    assert (await _check(limiter, key)).allowed

    clock.advance(59.5)
    refused = await _check(limiter, key)

    assert refused.allowed is False
    assert refused.retry_after == 1


async def test_an_hourly_limit_is_not_truncated_to_five_minutes(clock, redis_client):
    """The 300s cap made the advertised wait a lie on every long window.

    A client that obeys a truncated Retry-After returns to be refused again,
    which is the thundering herd the cap was supposed to prevent.
    """
    limiter = await _limiter(redis_client, requests=1, window=3600)
    key = "10.2.0.3"

    assert (await _check(limiter, key)).allowed
    refused = await _check(limiter, key)

    assert refused.allowed is False
    assert refused.retry_after == 3600, refused.retry_after


async def test_two_refusals_at_the_same_instant_agree(clock, redis_client):
    """No jitter: the answer is a fact about the window, not a random draw.

    De-synchronization comes from the windows themselves — each client's oldest
    entry arrived at its own time — so randomness bought nothing and cost the
    caller a number it could not reconcile with the reset timestamp.
    """
    limiter = await _limiter(redis_client, requests=1, window=60)
    key = "10.2.0.4"

    assert (await _check(limiter, key)).allowed
    clock.advance(10)

    waits = {(await _check(limiter, key)).retry_after for _ in range(5)}

    assert waits == {50}, waits


async def test_two_clients_are_told_different_waits_at_the_same_instant(
    clock, redis_client
):
    """The per-client de-synchronization the jitter was approximating."""
    limiter = await _limiter(redis_client, requests=1, window=60)

    assert (await _check(limiter, "10.2.0.5")).allowed
    clock.advance(20)
    assert (await _check(limiter, "10.2.0.6")).allowed
    clock.advance(10)

    first = await _check(limiter, "10.2.0.5")
    second = await _check(limiter, "10.2.0.6")

    assert (first.retry_after, second.retry_after) == (30, 50)


async def test_the_blocked_reset_time_is_the_oldest_entry_plus_one_window(
    clock, redis_client
):
    """``reset_time`` and ``Retry-After`` must name the same instant."""
    limiter = await _limiter(redis_client, requests=1, window=60)
    key = "10.2.0.7"

    assert (await _check(limiter, key)).allowed
    clock.advance(25)
    refused = await _check(limiter, key)

    assert refused.reset_time.timestamp() == pytest.approx(T0 + 60)
    assert refused.reset_time.timestamp() == pytest.approx(
        clock.time() + refused.retry_after
    )


async def test_the_allowed_reset_time_is_the_oldest_entry_plus_one_window(
    clock, redis_client
):
    """Served responses advertise the same instant, not a rolling ``now + window``.

    A client tracking the reset instant across responses saw it march forward on
    every request and could never plan against it.
    """
    limiter = await _limiter(redis_client, requests=5, window=60)
    key = "10.2.0.8"

    first = await _check(limiter, key)
    assert first.reset_time.timestamp() == pytest.approx(T0 + 60)

    clock.advance(7)
    second = await _check(limiter, key)

    assert second.allowed
    assert second.reset_time.timestamp() == pytest.approx(
        T0 + 60
    ), "the reset instant moved with the clock instead of naming when quota frees"


async def test_an_empty_window_reports_a_full_window(clock, redis_client):
    """The fallback: nothing to age out, so a full window is the truth."""
    limiter = await _limiter(redis_client, requests=5, window=60)

    first = await _check(limiter, "10.2.0.9")

    assert first.reset_time.timestamp() == pytest.approx(T0 + 60)


async def test_the_status_path_reports_the_same_instant(clock, redis_client):
    """Status backs the same headers, so it must not disagree with enforcement."""
    limiter = await _limiter(redis_client, requests=5, window=60)
    key = "10.2.0.10"

    enforced = await _check(limiter, key)
    clock.advance(30)

    status = await limiter.get_rate_limit_status(key, LimitType.GLOBAL)

    assert status is not None
    assert status.current_count == 1
    assert status.reset_time.timestamp() == pytest.approx(
        enforced.reset_time.timestamp()
    )
    assert status.reset_time.timestamp() == pytest.approx(T0 + 60)


async def test_the_status_path_falls_back_when_the_window_is_empty(clock, redis_client):
    limiter = await _limiter(redis_client, requests=5, window=60)

    status = await limiter.get_rate_limit_status("10.2.0.11", LimitType.GLOBAL)

    assert status is not None
    assert status.current_count == 0
    assert status.reset_time.timestamp() == pytest.approx(T0 + 60)


async def test_the_script_returns_the_oldest_score_on_both_paths(clock, redis_client):
    """The contract the two derivations depend on, asserted directly.

    Reverting the script to a three-element return would otherwise fail with an
    unpacking error somewhere far from the change that caused it.
    """
    limiter = await _limiter(redis_client, requests=1, window=60)
    key = f"{limiter.key_prefix}:{LimitType.GLOBAL.value}:10.2.0.12"

    allowed = await limiter._window_script(
        keys=[key], args=[f"{T0 - 60:.6f}", f"{T0:.6f}", "member-a", 1, 120]
    )
    blocked = await limiter._window_script(
        keys=[key], args=[f"{T0 - 60:.6f}", f"{T0:.6f}", "member-b", 1, 120]
    )

    assert len(allowed) == 4 and len(blocked) == 4, (allowed, blocked)
    # Empty window on the way in: nothing to report yet.
    assert rate_limiter_module._parse_oldest_score(allowed[3]) is None
    # And once an entry exists, its score comes back on the blocked path.
    assert rate_limiter_module._parse_oldest_score(blocked[3]) == pytest.approx(T0)
