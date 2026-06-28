"""Regression tests for Redis-dependent middleware client wiring.

Root cause guarded here: Starlette middleware is constructed at *import time*,
before the lifespan startup creates the Redis client. The deduplication and
idempotency middlewares used to capture a client in ``__init__`` (always ``None``
at that point) and the dedup middleware read ``app.state.redis_client`` — an
attribute that was never populated anywhere. The net effect was that request
deduplication (and idempotency) were silently disabled in *every* deployment,
including K8s with a healthy real Redis.

The fix wires the shared client into ``app.state.redis_client`` in the lifespan
composition root and has both middlewares resolve it lazily on the first request.
These tests assert that lazy resolution works and that dedup actually activates.
"""

from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import pytest

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.infrastructure.redis_client import resolve_redis_client


def _request_with_state_client(redis_client):
    """Minimal stand-in exposing ``request.app.state.redis_client``."""
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(redis_client=redis_client))
    )


def _request_without_state_client():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


@pytest.mark.unit
def test_resolve_redis_client_priority_order():
    """Shared resolver: injected wins, then app.state, then factory fallback."""
    injected = fakeredis_aio.FakeRedis(decode_responses=True)
    state_client = fakeredis_aio.FakeRedis(decode_responses=True)

    # 1. Injected client takes precedence over everything else.
    assert (
        resolve_redis_client(
            _request_with_state_client(state_client), injected=injected
        )
        is injected
    )

    # 2. No injected client → app.state.redis_client.
    assert (
        resolve_redis_client(_request_with_state_client(state_client)) is state_client
    )

    # 3. Neither → central factory returns a working client (never None).
    assert resolve_redis_client(_request_without_state_client()) is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dedup_resolves_client_from_app_state():
    """Dedup middleware adopts the composition-root client and stays enabled."""
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    mw = DeduplicationMiddleware(
        app=lambda scope, receive, send: None,
        settings=get_development_protection_settings(),
    )

    await mw._initialize(_request_with_state_client(fake))

    assert mw._redis is fake
    assert mw._initialized is True
    assert mw._disabled is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dedup_falls_back_to_factory_when_state_missing():
    """With no app.state client, dedup builds a working client (never disabled)."""
    mw = DeduplicationMiddleware(
        app=lambda scope, receive, send: None,
        settings=get_development_protection_settings(),
    )

    await mw._initialize(_request_without_state_client())

    # Central factory always returns a working client (FakeRedis in standalone),
    # so the middleware must initialize active rather than disable itself.
    assert mw._redis is not None
    assert mw._initialized is True
    assert mw._disabled is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_dedup_detects_duplicate_after_lazy_init():
    """End-to-end: the resolved client makes dedup actually block a duplicate."""
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    mw = DeduplicationMiddleware(
        app=lambda scope, receive, send: None,
        settings=get_development_protection_settings(),
    )
    await mw._initialize(_request_with_state_client(fake))

    # Same session + endpoint + body hashes to the same dedup key.
    key = f"{mw.redis_key_prefix}:fixed-hash"
    first = await mw._check_redis_duplicate(key, ttl=60)
    second = await mw._check_redis_duplicate(key, ttl=60)

    assert first[0] is False  # first request is not a duplicate
    assert second[0] is True  # identical follow-up is detected as duplicate


@pytest.mark.asyncio
@pytest.mark.unit
async def test_idempotency_resolves_client_from_app_state():
    """Idempotency middleware adopts the composition-root client lazily."""
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    mw = IdempotencyMiddleware(app=lambda scope, receive, send: None)

    # Constructed without a client (mirrors import-time wiring).
    assert mw.redis_client is None
    assert mw._resolved is False

    mw._ensure_redis(_request_with_state_client(fake))

    assert mw.redis_client is fake
    assert mw._resolved is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_idempotency_falls_back_to_factory_when_state_missing():
    """With no app.state client, idempotency builds a working client."""
    mw = IdempotencyMiddleware(app=lambda scope, receive, send: None)

    mw._ensure_redis(_request_without_state_client())

    assert mw.redis_client is not None
    assert mw._resolved is True


@pytest.mark.unit
def test_idempotency_injected_client_is_kept():
    """An explicitly injected client (tests/cloud) is honoured, no re-resolution."""
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    mw = IdempotencyMiddleware(app=lambda scope, receive, send: None, redis_client=fake)

    assert mw._resolved is True

    # Resolution is a no-op when a client was injected.
    mw._ensure_redis(_request_without_state_client())
    assert mw.redis_client is fake
