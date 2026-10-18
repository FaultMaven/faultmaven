"""The revocation watermark gauge (#1479): the Redis keyspace count and its collector.

The contract suite (``test_revocation_store_contract.py``) holds the behaviour
both stores share. This module holds what is specific to the Redis arm — the
configurable prefix reaches SCAN as a glob, so its metacharacters must be
escaped — and the collector that publishes the count.
"""

from __future__ import annotations

import asyncio
import logging
import time

import fakeredis.aioredis as fakeredis_aio
import pytest

from faultmaven.modules.auth.infrastructure.metrics import revocation_metrics
from faultmaven.modules.auth.infrastructure.metrics.revocation_metrics import (
    RevocationWatermarkCollector,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
    escape_glob,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "prefix",
    [
        "revoked:token:",
        "rev[x]:",
        "rev*:",
        "rev?:",
        "rev\\:",
    ],
)
async def test_the_prefix_matches_only_itself(prefix):
    """Two live watermarks under the prefix; every decoy is excluded.

    Raw, ``rev[x]:`` and ``rev\\:`` count 0 and ``rev*:`` counts 3 (it also
    matches ``revXuser:z``-shaped keys), which is why the pattern is escaped.
    """
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    store = RedisTokenRevocationStore(redis, key_prefix=prefix)
    now = time.time()
    await store.revoke_user_tokens_before("a", now, ttl=60)
    await store.revoke_user_tokens_before("b", now, ttl=60)
    await store.add_revoked_token("user:c", ttl=60)  # jti whose value looks like one
    await redis.set("revXuser:z", "1")
    await redis.set("other:user:q", "1")
    await redis.set(f"{prefix}user:expired", "1", px=1)
    await asyncio.sleep(0.05)

    assert await store.count_user_watermarks() == 2


def test_escape_glob_escapes_the_backslash_first():
    assert escape_glob("a\\b*c?d[e]") == "a\\\\b\\*c\\?d\\[e\\]"


async def test_a_key_scan_returns_twice_counts_once():
    class _ScanTwice:
        def scan_iter(self, match=None, count=None):
            async def gen():
                for key in ("revoked:token:user:a", "revoked:token:user:a"):
                    yield key

            return gen()

    store = RedisTokenRevocationStore(_ScanTwice())

    assert await store.count_user_watermarks() == 1


class _Store:
    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def count_user_watermarks(self):
        self.calls += 1
        outcome = self._outcomes[min(self.calls, len(self._outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_refresh_sets_the_gauge_to_the_stores_count(monkeypatch):
    seen = []
    monkeypatch.setattr(
        revocation_metrics,
        "revocation_watermarks",
        type("G", (), {"set": staticmethod(seen.append)})(),
    )

    await RevocationWatermarkCollector().refresh(_Store(7))

    assert seen == [7]


async def test_a_failing_store_keeps_the_gauge_logs_the_class_and_loops(
    monkeypatch, caplog
):
    seen = []
    monkeypatch.setattr(
        revocation_metrics,
        "revocation_watermarks",
        type("G", (), {"set": staticmethod(seen.append)})(),
    )
    store = _Store(3, RuntimeError("SELECT secret-statement"), 4)

    with caplog.at_level(logging.WARNING, logger=revocation_metrics.__name__):
        task = asyncio.create_task(
            RevocationWatermarkCollector().run_periodic(store, interval=0.01)
        )
        for _ in range(200):
            if store.calls >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert seen[:2] == [3, 4]  # the failed iteration set nothing
    messages = [r.getMessage() for r in caplog.records]
    assert any("RuntimeError" in m for m in messages)
    assert not any("secret-statement" in m for m in messages)


async def test_cancellation_propagates():
    task = asyncio.create_task(
        RevocationWatermarkCollector().run_periodic(_Store(1), interval=60)
    )
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
