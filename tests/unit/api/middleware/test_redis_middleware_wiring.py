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

import faultmaven.api.middleware.rate_limiting as rate_limiting
from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter
from faultmaven.infrastructure.redis_client import resolve_redis_client

_GET_ASYNC_CLIENT = "faultmaven.infrastructure.redis_client.get_async_redis_client"


def _asgi_app(scope, receive, send):
    """Minimal ASGI callable; middleware never dispatches through it here."""
    return None


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


# --------------------------------------------------------------------------- #
# Rate limiting (fm#897)
#
# The limiter used to build its own client from a self-assembled URL. It now
# adopts the composition root's boot-validated client — which means it must
# also not close a client it does not own, and must not latch itself off after
# a single failed initialization.
# --------------------------------------------------------------------------- #


class _ClosableClient:
    """Stand-in for a real Redis client: answers ping, records being closed."""

    def __init__(self):
        self.closed = False

    async def ping(self):
        return True

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rate_limiter_adopts_the_client_from_app_state(monkeypatch):
    """The shared client is used as-is — no second client is constructed."""
    shared = fakeredis_aio.FakeRedis(decode_responses=True)
    constructed = []

    async def _record_construction(redis_url=None):
        constructed.append(redis_url)
        return fakeredis_aio.FakeRedis(decode_responses=True)

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _record_construction)

    mw = RateLimitMiddleware(
        app=_asgi_app, settings=get_development_protection_settings()
    )
    await mw._initialize(_request_with_state_client(shared))

    assert mw.rate_limiter._redis is shared
    assert mw._initialized is True
    assert constructed == [], "a second Redis client/pool was constructed"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_rate_limiter_builds_its_own_client_when_state_is_empty(monkeypatch):
    """With no shared client the limiter still falls back to the factory."""
    built = _ClosableClient()

    async def _construct(redis_url=None):
        return built

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _construct)

    mw = RateLimitMiddleware(
        app=_asgi_app, settings=get_development_protection_settings()
    )
    await mw._initialize(_request_without_state_client())

    assert mw.rate_limiter._redis is built
    assert mw._initialized is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_close_leaves_the_adopted_client_open():
    """Closing the shared client on middleware teardown would break everything.

    ``app.state.redis_client`` also backs sessions, token revocation,
    deduplication and idempotency.
    """
    shared = _ClosableClient()
    limiter = RedisRateLimiter()

    await limiter.initialize(client=shared)
    assert limiter._owns_client is False

    await limiter.close()

    assert shared.closed is False
    assert await shared.ping() is True  # still usable by every other subsystem


@pytest.mark.asyncio
@pytest.mark.unit
async def test_close_closes_a_client_the_limiter_constructed(monkeypatch):
    """Ownership is real, not a blanket refusal to close: own pools are freed."""
    own = _ClosableClient()

    async def _construct(redis_url=None):
        return own

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _construct)

    limiter = RedisRateLimiter()
    await limiter.initialize()
    assert limiter._owns_client is True

    await limiter.close()

    assert own.closed is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_initialization_does_not_latch_and_retries_after_the_cooldown():
    """One blip must not disable rate limiting for the pod's whole lifetime.

    Also pins the other half: the retry is bounded, so a persistent outage does
    not attempt a connection on every single request.
    """
    attempts = []

    class _FlakyLimiter:
        async def initialize(self, client=None):
            attempts.append(client)
            if len(attempts) == 1:
                raise ConnectionError("redis down")

    settings = get_development_protection_settings()
    assert settings.fail_open_on_redis_error is True

    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    mw.rate_limiter = _FlakyLimiter()
    request = _request_without_state_client()

    # First attempt fails — and must NOT mark the middleware initialized.
    await mw._initialize(request)
    assert mw._initialized is False, "failure latched: rate limiting is off for good"
    assert mw._init_failed_at is not None
    assert len(attempts) == 1

    # An immediate retry is suppressed by the cooldown (no per-request storm).
    await mw._initialize(request)
    await mw._initialize(request)
    assert len(attempts) == 1, "retried inside the cooldown window"

    # Once the cooldown elapses the next request retries, and succeeds.
    mw._init_failed_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1
    await mw._initialize(request)

    assert len(attempts) == 2
    assert mw._initialized is True
    assert mw._init_failed_at is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fail_closed_deployments_still_refuse_while_uninitialized():
    """Not latching must not quietly turn a fail-closed deployment fail-open."""

    class _BrokenLimiter:
        async def initialize(self, client=None):
            raise ConnectionError("redis down")

    settings = get_development_protection_settings()
    settings.fail_open_on_redis_error = False

    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    mw.rate_limiter = _BrokenLimiter()
    request = _request_without_state_client()

    with pytest.raises(ConnectionError):
        await mw._initialize(request)

    # Inside the cooldown it still refuses rather than serving unlimited.
    with pytest.raises(RuntimeError):
        await mw._initialize(request)

    assert mw._initialized is False
