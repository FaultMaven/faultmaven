"""The protection middleware closes what it built, when the app stops serving.

Each Redis-holding middleware resolves its client lazily: the app's shared one
(``app.state.redis_client``, the container's), or, when the app serves without
one, a client the factory built for that middleware alone. Nothing closed the
second kind. On the Test Cloud job's real Redis that left one socket open per
test that served an app without its lifespan (``ResourceWarning: unclosed
transport``), and in production it is shutdown hygiene: a degraded standalone
boot is the case where the limiter owns a real client.

``shutdown_protection(app)``, the mirror of ``setup_protection_middleware``,
ends their service: the lifespan calls it first in its shutdown half. Each
middleware closes only the client it built, and is left resolving afresh.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.api.protection import (
    live_protection_middleware,
    setup_protection_middleware,
    shutdown_protection,
)
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.config.settings import Environment
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter

pytestmark = pytest.mark.unit

_RESOLVE = "faultmaven.infrastructure.redis_client.resolve_redis_client"


class _RecordingClient:
    """A stand-in real client: not from ``fakeredis``, records its closes."""

    def __init__(self, name="client"):
        self.name = name
        self.closes = 0

    async def ping(self):
        return True

    def register_script(self, script):
        async def _run(keys=None, args=None):
            raise AssertionError("never checked against here")

        return _run

    async def aclose(self):
        self.closes += 1


def _asgi_app(scope, receive, send):
    return None


def _request(state_client=None):
    state = SimpleNamespace()
    if state_client is not None:
        state.redis_client = state_client
    return SimpleNamespace(app=SimpleNamespace(state=state))


# ---------------------------------------------------------------------------
# The limiter: close() lets go and leaves it re-initializable
# ---------------------------------------------------------------------------


async def test_close_releases_and_closes_a_client_the_limiter_built():
    limiter = RedisRateLimiter()
    own = _RecordingClient("own")
    await limiter._adopt(own, owns=True, degraded=False)

    await limiter.close()

    assert own.closes == 1
    assert limiter._redis is None
    assert limiter._window_script is None
    assert limiter._owns_client is False


async def test_close_lets_go_of_the_shared_client_without_closing_it():
    limiter = RedisRateLimiter()
    shared = _RecordingClient("shared")
    await limiter._adopt(shared, owns=False, degraded=False)

    await limiter.close()

    assert shared.closes == 0
    assert limiter._redis is None


async def test_close_waits_for_a_dispatched_teardown():
    """A replaced pool's background close is not left pending at loop end."""
    limiter = RedisRateLimiter()
    released = asyncio.Event()

    class _HangingClient(_RecordingClient):
        async def aclose(self):
            await released.wait()
            await super().aclose()

    outgoing = _HangingClient("outgoing")
    await limiter._adopt(outgoing, owns=True, degraded=False)
    await limiter._adopt(_RecordingClient("replacement"), owns=True, degraded=False)
    assert limiter._teardown_tasks, "nothing was dispatched; the test proves nothing"

    asyncio.get_running_loop().call_soon(released.set)
    await asyncio.wait_for(limiter.close(), timeout=1.0)

    assert outgoing.closes == 1
    assert limiter._teardown_tasks == set()


async def test_a_second_close_does_nothing():
    limiter = RedisRateLimiter()
    own = _RecordingClient("own")
    await limiter._adopt(own, owns=True, degraded=False)

    await limiter.close()
    await limiter.close()

    assert own.closes == 1


async def test_the_next_initialize_builds_afresh(monkeypatch):
    """Keeping the closed client would not keep it closed: redis-py reconnects
    a closed pool on its next command."""
    import faultmaven.infrastructure.redis_client as rc

    built = []

    async def _factory(redis_url=None):
        client = _RecordingClient(f"built-{len(built)}")
        built.append(client)
        return client

    monkeypatch.setattr(rc, "get_async_redis_client", _factory)
    limiter = RedisRateLimiter()
    await limiter.initialize(client=None)
    first = limiter._redis

    await limiter.close()
    await limiter.initialize(client=None)

    assert first.closes == 1
    assert limiter._redis is built[1]
    assert limiter._owns_client is True
    assert limiter._window_script is not None


# ---------------------------------------------------------------------------
# The middleware: shutdown() closes the limiter and resets the latch
# ---------------------------------------------------------------------------


async def test_rate_limit_shutdown_closes_the_limiter_and_resets_the_latch():
    middleware = RateLimitMiddleware(_asgi_app, get_development_protection_settings())
    own = _RecordingClient("own")
    await middleware.rate_limiter._adopt(own, owns=True, degraded=False)
    middleware._initialized = True
    middleware._ever_initialized = True
    middleware._degraded = True
    middleware._last_attempt_at = 123.0
    middleware.rate_limiter._demotion_generation = 4

    await middleware.shutdown()

    assert own.closes == 1
    assert middleware.rate_limiter._redis is None
    # Otherwise dispatch short-circuits on _initialized against the closed client.
    assert middleware._initialized is False
    assert middleware._ever_initialized is False
    assert middleware._degraded is False
    assert middleware._last_attempt_at is None
    assert middleware._handled_demotion_generation == 4


@pytest.mark.parametrize(
    "middleware_cls, client_attr",
    [(DeduplicationMiddleware, "_redis"), (IdempotencyMiddleware, "redis_client")],
)
async def test_a_client_built_for_the_middleware_is_closed_at_shutdown(
    monkeypatch, middleware_cls, client_attr
):
    built = _RecordingClient("built")
    monkeypatch.setattr(
        _RESOLVE, lambda request, injected=None, **kw: injected or built
    )
    middleware = _new(middleware_cls)

    await _resolve(middleware, _request())
    await middleware.shutdown()

    assert built.closes == 1
    assert getattr(middleware, client_attr) is None


@pytest.mark.parametrize(
    "middleware_cls", [DeduplicationMiddleware, IdempotencyMiddleware]
)
async def test_the_apps_shared_client_is_never_closed(monkeypatch, middleware_cls):
    shared = _RecordingClient("shared")
    middleware = _new(middleware_cls)

    await _resolve(middleware, _request(state_client=shared))
    await middleware.shutdown()
    await middleware.shutdown()

    assert shared.closes == 0


@pytest.mark.parametrize(
    "middleware_cls, client_attr",
    [(DeduplicationMiddleware, "_redis"), (IdempotencyMiddleware, "redis_client")],
)
async def test_an_injected_client_is_kept_and_not_closed(middleware_cls, client_attr):
    injected = _RecordingClient("injected")
    middleware = _new(middleware_cls, redis_client=injected)

    await _resolve(middleware, _request())
    await middleware.shutdown()

    assert injected.closes == 0
    assert getattr(middleware, client_attr) is injected


def _new(middleware_cls, redis_client=None):
    if middleware_cls is DeduplicationMiddleware:
        return DeduplicationMiddleware(
            _asgi_app, get_development_protection_settings(), redis_client=redis_client
        )
    return IdempotencyMiddleware(_asgi_app, redis_client=redis_client)


async def _resolve(middleware, request):
    if isinstance(middleware, DeduplicationMiddleware):
        await middleware._initialize(request)
    else:
        middleware._ensure_redis(request)


# ---------------------------------------------------------------------------
# shutdown_protection: the lifespan's entry point
# ---------------------------------------------------------------------------


async def test_shutdown_protection_reaches_every_built_middleware(monkeypatch):
    app = FastAPI()
    setup_protection_middleware(app, environment=Environment.PRODUCTION)
    app.add_middleware(IdempotencyMiddleware)
    app.middleware_stack = app.build_middleware_stack()

    calls = []
    for cls in (RateLimitMiddleware, DeduplicationMiddleware, IdempotencyMiddleware):

        async def _shutdown(self, _cls=cls):
            calls.append(_cls.__name__)

        monkeypatch.setattr(cls, "shutdown", _shutdown)

    await shutdown_protection(app)

    assert sorted(calls) == sorted(
        type(m).__name__ for m in live_protection_middleware(app)
    )
    assert {"RateLimitMiddleware", "IdempotencyMiddleware"} <= set(calls)


async def test_shutdown_protection_before_the_app_ever_served_does_nothing():
    app = FastAPI()
    setup_protection_middleware(app, environment=Environment.PRODUCTION)
    assert app.middleware_stack is None

    await shutdown_protection(app)
