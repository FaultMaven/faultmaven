"""A key Redis holds is live, whatever the app clock reads (#2514).

Liveness was ``max(0, int(ttl - elapsed))`` from the stored timestamp and the
app's clock, and a 0 let the repeat run unclaimed while Redis held the key: in
the TTL's last second (``int`` truncates) and on a replica a TTL ahead. Now a
found key is refused and ``Retry-After`` is its ``PTTL``, rounded up, at least 1.
The stamp and the key's TTL are set directly in a FakeRedis behind the real
middleware, so no wall clock decides an outcome. Pinned elsewhere: a released
key's repeat runs and claims, and a re-sent own claim is no duplicate
(``test_dedup_claim_released_unless_success_2508.py``); the 409's labels
(``test_duplicate_gets_a_labelled_409``).
"""

from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, Response

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.utils.serialization import to_json_compatible

pytestmark = [pytest.mark.unit, pytest.mark.security]

PATH = "/api/v1/cases/case-2514"


class _Dedup:
    """A ``DELETE`` answered 204 behind the real middleware, over a FakeRedis."""

    def __init__(self):
        self.entries = 0
        self.redis = fakeredis_aio.FakeRedis(decode_responses=True)
        settings = get_development_protection_settings()
        settings.deduplication_enabled = True
        settings.fail_open_on_redis_error = False
        self.ttl = settings.deduplication["default"].ttl
        app = FastAPI()
        app.state.redis_client = self.redis
        app.add_middleware(DeduplicationMiddleware, settings=settings)

        @app.delete("/api/v1/cases/{case_id}")
        async def _delete(case_id: str):
            self.entries += 1
            return Response(status_code=204)

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def delete(self) -> httpx.Response:
        return await self.client.delete(PATH, headers={"X-Session-ID": "sess-2514"})

    async def claimed_key(self) -> str:
        """Send the first request, answered 204 so its claim stands; its key."""
        assert (await self.delete()).status_code == 204
        [key] = await self.redis.keys("*")
        return key

    async def restamp(self, key: str, seconds_ago: float, ttl_ms: int) -> datetime:
        """Store a claim stamped ``seconds_ago`` with ``ttl_ms`` of life left."""
        stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        value = f"{to_json_compatible(stamp)}|first-request"
        await self.redis.set(key, value, px=ttl_ms)
        return stamp


def _assert_duplicate(response: httpx.Response, retry_after: int) -> None:
    assert response.status_code == 409, response.text
    assert response.headers["x-error-code"] == "DUPLICATE_REQUEST"
    assert response.headers["Retry-After"] == str(retry_after)
    assert response.json()["retry_after"] == retry_after


async def test_a_live_key_stamped_a_whole_ttl_ago_is_refused():
    """Clock skew: a replica a TTL ahead of the claimer reads the key as expired."""
    dedup = _Dedup()
    async with dedup.client:
        key = await dedup.claimed_key()
        await dedup.restamp(key, seconds_ago=dedup.ttl + 5, ttl_ms=20_000)

        _assert_duplicate(await dedup.delete(), retry_after=20)
    assert dedup.entries == 1


async def test_a_key_in_its_last_second_is_refused_with_retry_after_1():
    """``int(ttl - elapsed)`` read 0 here and let the repeat run unclaimed."""
    dedup = _Dedup()
    async with dedup.client:
        key = await dedup.claimed_key()
        await dedup.restamp(key, seconds_ago=dedup.ttl - 0.5, ttl_ms=500)

        _assert_duplicate(await dedup.delete(), retry_after=1)
    assert dedup.entries == 1


async def test_retry_after_is_the_keys_remaining_life_rounded_up():
    """12.3 s left is ``Retry-After: 13``; the stamp feeds only the message."""
    dedup = _Dedup()
    async with dedup.client:
        key = await dedup.claimed_key()
        stamp = await dedup.restamp(key, seconds_ago=5, ttl_ms=12_300)

        refused = await dedup.delete()
        _assert_duplicate(refused, retry_after=13)
        assert refused.json()["message"] == (
            f"Duplicate request detected. Original request at {stamp}. TTL: 13s."
        )
    assert dedup.entries == 1


async def test_a_key_expired_in_redis_lets_the_repeat_run_and_claim():
    """Pinned: a key Redis no longer holds is not a duplicate."""
    dedup = _Dedup()
    async with dedup.client:
        # An expiry in the past expires the key at once, on Redis's own clock.
        await dedup.redis.pexpireat(await dedup.claimed_key(), 1)

        assert (await dedup.delete()).status_code == 204
        assert dedup.entries == 2
        # It took its own claim, so the next repeat is refused.
        refused = await dedup.delete()
        assert refused.status_code == 409
        assert refused.headers["x-error-code"] == "DUPLICATE_REQUEST"
    assert dedup.entries == 2
