"""The Redis-backed stores, driven against a real Redis server (#2243).

The suite steps of Test Standalone and Test Cloud set ``SKIP_SERVICE_CHECKS`` and
name no Redis, so every Redis consumer in them resolves the in-process FakeRedis.
Test Cloud's Redis service is the cloud pairing: a real server read through the
``hiredis`` parser ``requirements/cloud.txt`` pins (fm#1031). Two steps of that
job name it: ``scripts/check_redis_pairing.py``, and the step that runs this
module alone. This module drives the stores' commands and their server-side
scripts against it.

**Gate.** Marked ``cloud``, so Test Standalone deselects it. It runs only where
``REDIS_HOST`` or ``REDIS_URL`` names a server, and skips elsewhere. Where one is
named and does not answer, each test FAILS: ``get_async_redis_client()`` falls
back to FakeRedis silently, and a broken service container must not turn this
module green on the stand-in.

**Isolation.** Each test builds its own client on its own event loop, writes
only under a namespace no other test uses, deletes what it wrote, and closes
the client.
"""

import importlib.util
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import Body, FastAPI
from redis.utils import HIREDIS_AVAILABLE

from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.config.idempotency_key import IDEMPOTENCY_REPLAYED_HEADER
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter
from faultmaven.infrastructure.redis_client import (
    get_async_redis_client,
    is_fakeredis,
)
from faultmaven.models.common import SessionContext
from faultmaven.models.protection import LimitType, RateLimitConfig, RateLimitSpec
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
    RedisTokenRevocationStore,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.cloud,
    pytest.mark.skipif(
        not (os.environ.get("REDIS_HOST") or os.environ.get("REDIS_URL")),
        reason="neither REDIS_HOST nor REDIS_URL names a Redis to drive "
        "(Test Cloud's real-Redis step sets REDIS_HOST)",
    ),
]


def _named_server() -> str:
    """The variable naming this run's Redis; ``REDIS_URL`` wins, as in the factory."""
    if os.environ.get("REDIS_URL"):
        return "REDIS_URL"  # its value can carry a password, so it is not shown
    port = os.environ.get("REDIS_PORT", "6379")
    return f"REDIS_HOST={os.environ['REDIS_HOST']!r} REDIS_PORT={port!r}"


def _pairing_check():
    """``scripts/check_redis_pairing.py``, loaded by path: ``scripts/`` is no package."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_redis_pairing.py"
    spec = importlib.util.spec_from_file_location("check_redis_pairing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def real_redis():
    """A client of this test's own on the named server, or a failure."""
    client = await get_async_redis_client()
    if is_fakeredis(client):
        pytest.fail(
            f"{_named_server()} is set, but get_async_redis_client() resolved the "
            "in-process FakeRedis: the server did not answer. This module exists to "
            "drive that server, so it fails rather than skips; check that the "
            "Redis service is up.",
            pytrace=False,
        )
    yield client
    await client.aclose()


@pytest.fixture
async def namespace(real_redis):
    """A key prefix no other test uses; every key under it is deleted afterwards."""
    prefix = f"fm-test-real-redis:{uuid.uuid4().hex}:"
    yield prefix
    keys = [key async for key in real_redis.scan_iter(match=f"{prefix}*", count=1000)]
    if keys:
        await real_redis.delete(*keys)


async def test_a_pooled_connection_parses_with_hiredis_when_redis_py_can(
    real_redis, namespace
):
    key = f"{namespace}parser"
    await real_redis.set(key, "ok", ex=60)
    assert await real_redis.get(key) == "ok"

    # The pairing check's own reading, so the module and the CI step agree.
    parser = _pairing_check()._live_parser_class(real_redis)

    if HIREDIS_AVAILABLE:
        assert parser.__name__ == "_AsyncHiredisParser", parser
    else:
        assert "hiredis" not in parser.__module__, parser


async def test_the_session_store_saves_reads_expires_and_deletes(real_redis, namespace):
    store = RedisSessionStore(real_redis)
    store.prefix = f"{namespace}session:"

    await store.save(SessionContext(session_id="session-1", user_id="user-1"))

    stored = await store.get_session("session-1")
    assert stored is not None
    assert (stored.session_id, stored.user_id) == ("session-1", "user-1")
    assert 0 < await real_redis.ttl(f"{store.prefix}session-1") <= store.default_ttl

    assert await store.delete_session("session-1") is True
    assert await store.get_session("session-1") is None


async def test_the_session_store_deletes_only_the_keys_holding_the_value(
    real_redis, namespace
):
    """``delete_keys_holding``: SCAN plus the compare-and-delete script."""
    store = RedisSessionStore(real_redis)
    store.prefix = f"{namespace}session:"
    await store.set("s-1:current_case_id", "case-a")
    await store.set("s-2:current_case_id", "case-a")
    await store.set("s-3:current_case_id", "case-b")

    deleted = await store.delete_keys_holding("*:current_case_id", "case-a")

    assert deleted == 2
    assert await store.get("s-1:current_case_id") is None
    assert await store.get("s-2:current_case_id") is None
    # The positive control: a pointer holding another value is kept.
    assert await store.get("s-3:current_case_id") == "case-b"


async def test_the_revocation_store_answers_per_token_and_per_user(
    real_redis, namespace
):
    store = RedisTokenRevocationStore(
        real_redis, key_prefix=f"{namespace}revoked:token:"
    )

    await store.add_revoked_token("jti-revoked", ttl=60)

    assert await store.is_revoked("jti-revoked") is True
    assert await store.is_revoked("jti-live") is False

    watermark = time.time()
    await store.revoke_user_tokens_before("user-1", watermark, ttl=60)

    assert await store.is_user_revoked("user-1", issued_at=int(watermark) - 1)
    assert not await store.is_user_revoked("user-1", issued_at=int(watermark) + 1)
    assert not await store.is_user_revoked("user-2", issued_at=int(watermark) - 1)
    assert await store.count_user_watermarks() == 1


async def test_the_revocation_store_clears_only_a_watermark_older_than_the_instant(
    real_redis, namespace
):
    """``clear_user_revocation_if_before``: the server-side compare-and-delete."""
    store = RedisTokenRevocationStore(
        real_redis, key_prefix=f"{namespace}revoked:token:"
    )
    watermark = time.time()
    await store.revoke_user_tokens_before("user-1", watermark, ttl=60)

    # The positive control: an instant that is not later leaves it in place.
    assert await store.clear_user_revocation_if_before("user-1", watermark) is False
    assert await store.is_user_revoked("user-1", issued_at=int(watermark) - 1)

    assert await store.clear_user_revocation_if_before("user-1", watermark + 1)
    assert not await store.is_user_revoked("user-1", issued_at=int(watermark) - 1)
    assert await store.count_user_watermarks() == 0


async def test_the_rate_limiter_window_script_admits_then_refuses(
    real_redis, namespace
):
    limiter = RedisRateLimiter(key_prefix=f"{namespace}rl", fallback_enabled=False)
    await limiter.initialize(client=real_redis)
    limiter.configure_limits(
        {LimitType.GLOBAL.value: RateLimitConfig(requests=3, window=60)}
    )
    spec = RateLimitSpec(key="client-1", limit_type=LimitType.GLOBAL)

    try:
        # The positive control: under the limit, each request is admitted and
        # counted, and the quota it advertises falls by one each time.
        for served in (1, 2, 3):
            [admitted] = await limiter.check_rate_limits([spec])
            assert admitted.allowed, admitted
            remaining = admitted.limit - admitted.current_count
            assert (admitted.limit, remaining) == (3, 3 - served)

        [refused] = await limiter.check_rate_limits([spec])
    finally:
        await limiter.close()

    assert not refused.allowed, refused
    assert (refused.limit, refused.current_count) == (3, 3)
    assert refused.retry_after is not None and 0 < refused.retry_after <= 60


async def test_idempotency_stores_a_keyed_response_and_replays_its_retry(
    real_redis, namespace
):
    app = FastAPI()
    calls = []

    @app.post("/api/v1/cases")
    async def create_case(payload: dict = Body(default={})):
        calls.append(payload)
        return {"created": len(calls), "title": payload.get("title")}

    def namespaced_idempotency(inner, *, redis_client):
        # The production middleware, with its keys under this test's namespace.
        middleware = IdempotencyMiddleware(inner, redis_client=redis_client)
        middleware.key_prefix = f"{namespace}idempotency:"
        return middleware

    app.add_middleware(namespaced_idempotency, redis_client=real_redis)
    headers = {"Authorization": "Bearer caller", "Idempotency-Key": str(uuid.uuid4())}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
        first = await client.post("/api/v1/cases", headers=headers, json={"title": "t"})
        stored = [
            key async for key in real_redis.scan_iter(match=f"{namespace}idempotency:*")
        ]
        retry = await client.post("/api/v1/cases", headers=headers, json={"title": "t"})

    assert first.status_code == 200
    assert IDEMPOTENCY_REPLAYED_HEADER not in first.headers
    assert len(stored) == 1, "the first keyed request must be stored on the server"

    assert retry.status_code == 200
    assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert retry.json() == first.json() == {"created": 1, "title": "t"}
    assert len(calls) == 1, "the replayed retry must not reach the route"
