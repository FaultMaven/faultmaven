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

import asyncio
import itertools
import logging
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

import faultmaven.api.middleware.rate_limiting as rate_limiting
import faultmaven.infrastructure.protection.rate_limiter as rate_limiter_module
from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter
from faultmaven.infrastructure.redis_client import (
    RedisUnavailableError,
    resolve_redis_client,
)
from faultmaven.models.protection import RateLimitConfig

_GET_ASYNC_CLIENT = "faultmaven.infrastructure.redis_client.get_async_redis_client"


@pytest.fixture(autouse=True)
def _isolated_fakeredis():
    """Give each test its own stand-in singleton.

    ``get_fakeredis_client`` caches one process-wide instance, and an async
    FakeRedis binds to the event loop that created it. Reused across tests it
    raises "bound to a different event loop" — which these tests' own code paths
    would then read as a *check failure*, silently manufacturing the very
    condition under test.
    """
    from faultmaven.infrastructure.redis_client import reset_fakeredis_client

    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


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

    def register_script(self, script):
        async def _run(keys=None, args=None):
            raise AssertionError("this stand-in is never checked against")

        return _run

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
        is_degraded = False
        demotion_generation = 0

        async def initialize(self, client=None, verify_client=False):
            attempts.append(client)
            if len(attempts) == 1:
                raise ConnectionError("redis down")

    settings = get_development_protection_settings()
    assert settings.fail_open_on_redis_error is True

    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    mw.rate_limiter = _FlakyLimiter()
    request = _request_without_state_client()

    # First attempt fails — and must NOT mark the middleware initialized.
    assert await mw._initialize(request) is False
    assert mw._initialized is False, "failure latched: rate limiting is off for good"
    assert mw._last_attempt_at is not None
    assert len(attempts) == 1

    # An immediate retry is suppressed by the cooldown (no per-request storm).
    await mw._initialize(request)
    await mw._initialize(request)
    assert len(attempts) == 1, "retried inside the cooldown window"

    # Once the cooldown elapses the next request retries, and succeeds.
    mw._last_attempt_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1
    assert await mw._initialize(request) is True

    assert len(attempts) == 2
    assert mw._initialized is True
    assert mw._last_attempt_at is None


# --------------------------------------------------------------------------- #
# The initialization lifecycle, observed through ``dispatch`` (fm#897 review)
#
# The unifying defect these guard: the middleware claimed "initialization does
# not latch on failure" while, in the default configuration, it still did.
# ``RedisRateLimiter.initialize`` returned normally with ``_redis is None``
# whenever ``fallback_enabled`` was true, the middleware marked itself
# initialized, and every later check hit ``None.eval(...)`` → fail-open, for the
# pod's whole lifetime. Each test below asserts the *observable* property
# (what a later request gets), not an internal flag.
# --------------------------------------------------------------------------- #

_IP_COUNTER = itertools.count(1)


def _unique_client():
    """A fresh client IP per test.

    The rate-limit key is derived from the client IP, and the degraded rung
    shares one process-wide FakeRedis, so reused IPs would let one test's
    counters decide another's verdict.
    """
    return (f"10.0.0.{next(_IP_COUNTER)}", 1234)


def _limited_settings(global_requests=1, fail_open=True):
    """Development settings with a deliberately tiny global limit.

    The limit has to be small enough that "is this pod still enforcing?" is
    answerable in two requests.
    """
    settings = get_development_protection_settings()
    settings.fail_open_on_redis_error = fail_open
    settings.rate_limits = {
        "global": RateLimitConfig(enabled=True, requests=global_requests, window=60)
    }
    return settings


def _http_request(path="/api/v1/cases", app=None, headers=None, client=None):
    """A real Starlette request — ``dispatch`` reads path, headers and client."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": client or ("10.0.0.254", 1234),
        "app": app if app is not None else SimpleNamespace(state=SimpleNamespace()),
    }
    return Request(scope)


def _app(redis_client=None):
    state = SimpleNamespace()
    if redis_client is not None:
        state.redis_client = redis_client
    return SimpleNamespace(state=state)


async def _call_next(request):
    return PlainTextResponse("ok")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_failed_init_never_reports_success_nor_latches_unlimited(monkeypatch):
    """F2: ``fallback_enabled=True`` must not make a ``None`` client look fine.

    ``fallback_enabled`` governs whether a *degraded* client is acceptable. It
    is not a licence to report success with no client at all — that is what
    latched the pod into "no rate limiting, ever".
    """
    down = True
    shared = fakeredis_aio.FakeRedis(decode_responses=True)

    async def _factory(redis_url=None):
        if down:
            raise ConnectionError("redis down")
        return shared

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    assert mw.rate_limiter.fallback_enabled is True

    # The limiter contract itself: no usable client ⇒ it raises, whatever the
    # fallback flag says, and leaves nothing behind that looks usable.
    with pytest.raises(ConnectionError):
        await mw.rate_limiter.initialize()
    assert mw.rate_limiter._redis is None

    ip = _unique_client()

    # Through the middleware: fail-open passes the request, but must not latch.
    first = await mw.dispatch(_http_request(app=_app(), client=ip), _call_next)
    assert first.status_code == 200
    assert mw._initialized is False, "latched with no client — unlimited for good"

    # Redis comes back. Past the cooldown the pod enforces again — the very
    # thing the latch made impossible for the rest of the pod's life.
    down = False
    mw._last_attempt_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1

    allowed = await mw.dispatch(_http_request(app=_app(), client=ip), _call_next)
    limited = await mw.dispatch(_http_request(app=_app(), client=ip), _call_next)

    assert allowed.status_code == 200
    assert limited.status_code == 429, "the pod is not enforcing limits again"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_the_degraded_rung_keeps_enforcing_and_is_promoted_back(monkeypatch):
    """F3: the per-replica stand-in serves now, but must not be the end state.

    Landing on FakeRedis used to set ``_initialized`` and latch, so the log line
    "per-replica until Redis is reachable" was a promise the code never kept.
    """
    unavailable = True
    shared = fakeredis_aio.FakeRedis(decode_responses=True)

    async def _factory(redis_url=None):
        if unavailable:
            raise RedisUnavailableError("cloud refuses the in-process stand-in")
        return shared

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    # Degraded, but serving — and still enforcing: no window of free traffic.
    allowed = await mw.dispatch(_http_request(app=_app(), client=ip), _call_next)
    limited = await mw.dispatch(_http_request(app=_app(), client=ip), _call_next)

    assert allowed.status_code == 200
    assert limited.status_code == 429, "the stand-in is not enforcing limits"
    assert mw._initialized is True
    assert mw._degraded is True
    assert mw.rate_limiter.is_degraded is True
    assert mw._last_attempt_at is not None, "degraded rung latched: no way back"

    # Redis returns. The next request past the cooldown promotes the limiter.
    unavailable = False
    mw._last_attempt_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1
    await mw.dispatch(_http_request(app=_app(), client=_unique_client()), _call_next)

    assert mw.rate_limiter._redis is shared
    assert mw._degraded is False
    assert mw._last_attempt_at is None, "promoted, but still re-attempting"


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("path", ["/health", "/health/dependencies", "/readiness"])
async def test_a_probe_is_never_503d_while_redis_is_down(monkeypatch, path):
    """F1: a Redis blip must not get the pod killed by its own kubelet.

    ``_initialize`` used to run before the bypass check and raise inside the
    cooldown, which ``dispatch`` turned into a 503 on *every* route for 30s —
    liveness and readiness included.
    """
    attempts = []

    async def _factory(redis_url=None):
        attempts.append(redis_url)
        raise ConnectionError("redis down")

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    # Fail closed: the configuration in which the 503 cliff actually bit.
    settings = _limited_settings(fail_open=False)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)

    response = await mw.dispatch(
        _http_request(path=path, app=_app(), client=_unique_client()), _call_next
    )

    assert response.status_code == 200
    assert attempts == [], "a probe triggered a Redis connection attempt"
    assert mw._last_attempt_at is None

    # Not vacuous: the same middleware, same outage, does refuse a real route.
    refused = await mw.dispatch(
        _http_request(app=_app(), client=_unique_client()), _call_next
    )
    assert refused.status_code == 503


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("mode", ["disabled", "bypass_header"])
async def test_unlimited_requests_never_trigger_initialization(monkeypatch, mode):
    """F1: "is this limited at all" is resolved before anything Redis-dependent."""
    attempts = []

    async def _factory(redis_url=None):
        attempts.append(redis_url)
        raise ConnectionError("redis down")

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(fail_open=False)
    headers = None
    if mode == "disabled":
        settings.rate_limiting_enabled = False
    else:
        headers = {settings.protection_bypass_headers[0]: "1"}

    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    response = await mw.dispatch(
        _http_request(app=_app(), headers=headers, client=_unique_client()),
        _call_next,
    )

    assert response.status_code == 200
    assert attempts == [], "initialization ran for a request that is never limited"
    assert mw._last_attempt_at is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_per_request_error_spam_while_uninitialized(monkeypatch, caplog):
    """F5: the degrade is logged once per cooldown window, not per request.

    The check used to run against a ``None`` client, emitting one to four ERROR
    lines per request for the whole 30s window.
    """

    async def _factory(redis_url=None):
        raise ConnectionError("redis down")

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(fail_open=True)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)

    request_count = 25
    with caplog.at_level(logging.ERROR):
        for _ in range(request_count):
            response = await mw.dispatch(
                _http_request(app=_app(), client=_unique_client()), _call_next
            )
            assert response.status_code == 200

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) <= 2, (
        f"{len(errors)} ERROR lines for {request_count} requests — the degrade "
        "is being logged per request, not per cooldown window"
    )
    assert any("no Redis client" in r.getMessage() for r in errors)


# --------------------------------------------------------------------------- #
# Mid-life client death (fm#897 adversarial review, B1)
#
# The dominant production outage is not "Redis was down when the pod booted" —
# it is "the pod booted fine and Redis restarted / failed over / lost the
# network an hour later". The short-circuit on `_initialized and not _degraded`
# made a successful adoption permanent, so in that shape the limiter kept a dead
# client, every check fell through `check_rate_limit`'s catch-all to fail-open,
# and NO rung of the degrade ladder was ever reached. The doc's claim that
# "rung 2 exists precisely so rung 3 is nearly unreachable" — the claim that
# justifies defaulting production to fail-open — was false here.
# --------------------------------------------------------------------------- #


class _DeadClient:
    """A client that was adopted while healthy and has since stopped answering."""

    def __init__(self):
        self.eval_calls = 0
        self.ping_calls = 0

    async def ping(self):
        self.ping_calls += 1
        raise ConnectionError("connection reset by peer")

    def register_script(self, script):
        """Registering succeeds; running the script is what fails.

        redis-py's ``register_script`` is a local operation — it only precomputes
        the SHA — so a dead server cannot make it fail. The failure surfaces on
        the EVALSHA, i.e. on the check path.
        """

        async def _run(keys=None, args=None):
            self.eval_calls += 1
            raise ConnectionError("connection reset by peer")

        return _run

    async def zcount(self, *args, **kwargs):
        raise ConnectionError("connection reset by peer")


async def _drive(mw, ip, count, state_client=None):
    """Send ``count`` requests from one client IP; return the status codes.

    ``state_client`` is what ``app.state.redis_client`` holds for the duration.
    On a mid-life outage that is the client that just died — the composition
    root does not withdraw it — so the tests must present it, not ``None``.
    """
    return [
        (
            await mw.dispatch(
                _http_request(app=_app(state_client), client=ip), _call_next
            )
        ).status_code
        for _ in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_client_that_dies_mid_life_demotes_and_keeps_enforcing(monkeypatch):
    """B1: sustained check failures must re-enter the ladder, not fail open forever.

    The observable property, at the reviewer's altitude: after the adopted
    client dies, a later request is still *rate limited*. Before the fix this
    probe returned 200 for every request indefinitely.
    """
    dead = _DeadClient()

    async def _factory(redis_url=None):
        # Standalone's answer when the configured Redis is unreachable.
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    # Boot healthy on the shared client, then it dies.
    await mw._initialize(_http_request(app=_app(dead), client=ip))
    assert mw._initialized is True
    assert mw._degraded is False
    assert mw.rate_limiter._redis is dead

    # Requests during the failure run pass (a single timeout must not demote),
    # but the run is bounded: the limiter declares the client dead.
    statuses = await _drive(
        mw, ip, rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD, state_client=dead
    )
    assert statuses == [200] * len(statuses)
    assert mw.rate_limiter.demotion_generation == 1

    # The next request re-enters the ladder and lands on the stand-in, which
    # enforces limits — so the request after it is refused.
    after = await _drive(mw, ip, 2, state_client=dead)

    assert mw._degraded is True, "the ladder was not re-entered"
    assert after[-1] == 429, (
        f"still unlimited after the client died (statuses {after}) — the "
        "degrade ladder was never re-entered"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_dead_shared_client_is_not_blindly_re_adopted(monkeypatch):
    """B1: app.state holds the client that just died — prove it before adopting.

    Re-adopting it unchecked re-enters the dead state on every retry, so the
    stand-in below it is never reached.
    """
    dead = _DeadClient()
    built = []

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        built.append(redis_url)
        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    await mw._initialize(_http_request(app=_app(dead), client=ip))
    assert dead.ping_calls == 0, "the first adoption re-pinged a boot-validated client"

    await _drive(
        mw,
        ip,
        rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD + 1,
        state_client=dead,
    )

    assert dead.ping_calls >= 1, "the dead client was re-adopted without a ping"
    assert mw.rate_limiter._redis is not dead, "the dead client was re-adopted"
    assert built, "the ladder never fell through to the factory rung"
    assert mw._degraded is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_promotion_still_works_after_a_mid_life_demotion(monkeypatch):
    """B1: demotion must not be a one-way door — recovery is the point."""
    dead = _DeadClient()
    recovered = fakeredis_aio.FakeRedis(decode_responses=True)

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    await mw._initialize(_http_request(app=_app(dead), client=ip))
    await _drive(
        mw,
        ip,
        rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD + 1,
        state_client=dead,
    )
    assert mw._degraded is True

    # Redis comes back; the composition root's client answers again.
    mw._last_attempt_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1
    await mw._initialize(_http_request(app=_app(recovered), client=ip))

    assert mw.rate_limiter._redis is recovered
    assert mw._degraded is False
    assert mw._initialized is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_dying_client_does_not_log_per_request(monkeypatch, caplog):
    """D1: the check-path catch-all logged once per failed check, indefinitely.

    Steady state after the demotion is the stand-in, where checks succeed — so
    the bound here is "a handful of lines for the death", not "one per request".
    """
    dead = _DeadClient()

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    settings = _limited_settings(global_requests=10_000)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    await mw._initialize(_http_request(app=_app(dead), client=ip))

    request_count = 40
    with caplog.at_level(logging.ERROR):
        await _drive(mw, ip, request_count, state_client=dead)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) <= 5, (
        f"{len(errors)} ERROR lines for {request_count} requests — the failing "
        "check is being logged per request"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_an_isolated_check_failure_does_not_demote(monkeypatch):
    """The threshold is deliberate: one transient timeout is not a dead client."""
    shared = fakeredis_aio.FakeRedis(decode_responses=True)

    settings = _limited_settings(global_requests=10_000)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()
    await mw._initialize(_http_request(app=_app(shared), client=ip))

    limiter = mw.rate_limiter
    real_script = limiter._window_script
    calls = {"n": 0}

    async def _flaky_script(keys=None, args=None):
        calls["n"] += 1
        # Fail on every other call: never CHECK_FAILURE_DEMOTION_THRESHOLD in a
        # row, so an intermittent error must never accumulate into a demotion.
        if calls["n"] % 2 == 1:
            raise ConnectionError("transient timeout")
        return await real_script(keys=keys, args=args)

    # The check path snapshots ``_window_script``, so wrapping it here is the
    # seam the failure actually travels through — patching the client's raw
    # ``eval`` would miss the registered script's EVALSHA entirely.
    monkeypatch.setattr(limiter, "_window_script", _flaky_script)

    await _drive(mw, ip, 12, state_client=shared)

    assert calls["n"] >= 12, "the flaky script was never exercised — dead gate"
    assert limiter.demotion_generation == 0, "an intermittent error demoted the client"
    assert mw._degraded is False
    assert limiter._redis is shared


class _KillableClient:
    """A real-looking client that can be killed and revived mid-test."""

    def __init__(self, backing):
        self._backing = backing
        self.alive = True

    def _check(self):
        if not self.alive:
            raise ConnectionError("connection reset by peer")

    async def ping(self):
        self._check()
        return True

    def register_script(self, script):
        """Delegate registration, but gate every *run* on liveness.

        Registration is local (it only precomputes the SHA), so it keeps working
        while the server is dead; the death has to surface on the EVALSHA.
        """
        registered = self._backing.register_script(script)

        async def _run(keys=None, args=None):
            self._check()
            return await registered(keys=keys, args=args)

        return _run

    async def zcount(self, *args, **kwargs):
        self._check()
        return await self._backing.zcount(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_second_death_is_demoted_just_like_the_first(monkeypatch):
    """Demotion must stay armed across re-adoptions, not fire once per process.

    One death yields one generation bump because ``_client_declared_dead``
    latches for the current client. That is only correct because adopting a
    client clears both the flag and the failure run: leave either behind and the
    ladder is entered once and never again.
    """

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    killable = _KillableClient(fakeredis_aio.FakeRedis(decode_responses=True))
    settings = _limited_settings(global_requests=10_000)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()
    threshold = rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD

    await mw._initialize(_http_request(app=_app(killable), client=ip))
    assert mw.rate_limiter._redis is killable

    # First death → first demotion onto the stand-in.
    killable.alive = False
    await _drive(mw, ip, threshold + 1, state_client=killable)
    assert mw.rate_limiter.demotion_generation == 1
    assert mw._degraded is True

    # Redis recovers and the limiter is promoted back.
    killable.alive = True
    mw._last_attempt_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1
    await _drive(mw, ip, 1, state_client=killable)
    assert mw.rate_limiter._redis is killable
    assert mw._degraded is False

    # Second death → must be demoted again, not ignored.
    killable.alive = False
    await _drive(mw, ip, threshold + 1, state_client=killable)

    assert mw.rate_limiter.demotion_generation == 2, (
        "the second death was never detected — the failure run survived the "
        "re-adoption, so the counter passed the threshold without equalling it"
    )
    assert mw._degraded is True
    assert mw.rate_limiter._redis is not killable


class _DyingPoolClient:
    """A pool that dies under traffic: in-flight commands hang, later ones fail fast.

    That asymmetry is the real shape of a pool death, and it is what makes a
    failed check outlive the client it was issued against. Commands already on
    the wire block until ``socket_timeout`` (10s), while commands issued after
    the death fail immediately — so a failure *issued* before a demotion can
    *land* after it, against whatever client has replaced the dead one.

    ``register_script`` succeeds — registration is local (it only precomputes
    the SHA), so a dead server cannot make it fail. The hang-then-refuse
    surfaces on the EVALSHA, i.e. when the script is run.
    """

    def __init__(self):
        self._released = asyncio.Event()
        self.hang = True

    async def ping(self):
        raise ConnectionError("connection reset by peer")

    def register_script(self, script):
        async def _run(keys=None, args=None):
            if self.hang:
                await self._released.wait()  # stands for the socket_timeout wait
            raise ConnectionError("connection reset by peer")

        return _run

    def time_out_the_in_flight_commands(self):
        self._released.set()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_stale_check_failure_does_not_kill_the_healthy_successor(monkeypatch):
    """Failures must be attributed to the client they were issued against.

    Otherwise the fix for a dead client eats its own successor: the ladder
    re-enters onto a healthy stand-in, the commands that were on the wire when
    the pool died then time out, and those stale failures cross the threshold a
    second time. The middleware drops ``_initialized``, the cooldown gate has
    just been armed by the re-entry, so it refuses to re-initialize — and every
    request for the rest of the cooldown passes unlimited *while a healthy
    client sits in the limiter*.
    """
    dead = _DyingPoolClient()

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    threshold = rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD
    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    await mw._initialize(_http_request(app=_app(dead), client=ip))
    assert mw.rate_limiter._redis is dead

    # `threshold` requests are on the wire when the pool dies.
    in_flight = [
        asyncio.create_task(
            mw.dispatch(_http_request(app=_app(dead), client=ip), _call_next)
        )
        for _ in range(threshold)
    ]
    for _ in range(10):
        await asyncio.sleep(0)
    assert all(not task.done() for task in in_flight), "the probe never got in flight"

    # Traffic arriving after the death fails fast and declares the client dead.
    dead.hang = False
    await _drive(mw, ip, threshold, state_client=dead)
    assert mw.rate_limiter.demotion_generation == 1

    # The next request re-enters the ladder onto a healthy stand-in and is
    # counted against it (the global limit is 1).
    assert await _drive(mw, ip, 1, state_client=dead) == [200]
    assert mw._degraded is True
    successor = mw.rate_limiter._redis
    assert successor is not dead

    # Now the commands that were on the wire time out.
    dead.time_out_the_in_flight_commands()
    landed = await asyncio.gather(*in_flight)
    assert [response.status_code for response in landed] == [200] * threshold

    # The property, at the request's altitude: the successor is still enforcing.
    assert await _drive(mw, ip, 1, state_client=dead) == [429], (
        "unlimited traffic while a healthy client sits in the limiter — the "
        "stale failures re-entered the ladder and the cooldown blocked recovery"
    )
    assert mw.rate_limiter.demotion_generation == 1, (
        "stale failures from the dead client were counted against its "
        "replacement, so a healthy client was declared dead"
    )
    assert mw.rate_limiter._redis is successor
    assert mw._initialized is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_a_genuine_failure_run_on_the_current_client_still_demotes(monkeypatch):
    """The converse of the guard above: attribution must not suppress real deaths.

    Ignoring stale failures is only safe if failures from the *current* client
    are still counted. A check whose client is still installed must demote it,
    however many adoptions happened before that client arrived.
    """
    first = _DeadClient()
    second = _KillableClient(fakeredis_aio.FakeRedis(decode_responses=True))

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    threshold = rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD
    settings = _limited_settings(global_requests=10_000)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    # An adoption history: first client dies, the ladder demotes, a second real
    # client is adopted on promotion.
    await mw._initialize(_http_request(app=_app(first), client=ip))
    await _drive(mw, ip, threshold + 1, state_client=first)
    assert mw.rate_limiter.demotion_generation == 1
    assert mw._degraded is True

    mw._last_attempt_at -= rate_limiting.INIT_RETRY_COOLDOWN_SECONDS + 1
    await _drive(mw, ip, 1, state_client=second)
    assert mw.rate_limiter._redis is second
    assert mw._degraded is False

    # The current client now dies. Its own failures must still demote it.
    second.alive = False
    await _drive(mw, ip, threshold, state_client=second)

    assert mw.rate_limiter.demotion_generation == 2, (
        "failures from the client actually installed were ignored — attribution "
        "is suppressing genuine deaths, not just stale ones"
    )


# --------------------------------------------------------------------------- #
# Latent invariants no test observed (fm#897 adversarial review, M4 and M6)
#
# Both survived mutation: the code was right and nothing guarded it.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.unit
async def test_the_factory_returned_stand_in_is_never_owned(monkeypatch):
    """M4: the stand-in is a process-wide singleton — this limiter cannot own it.

    ``get_fakeredis_client`` returns one instance shared with sessions, token
    revocation, deduplication and idempotency. Marking it owned means a future
    ``close()`` caller takes all of them down together, so ownership has to be
    false at the point of adoption, not merely unused today.
    """
    from faultmaven.infrastructure.redis_client import get_fakeredis_client

    stand_in = get_fakeredis_client()

    async def _factory(redis_url=None):
        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    limiter = RedisRateLimiter()
    await limiter.initialize()

    assert limiter._redis is stand_in, "the factory rung did not yield the stand-in"
    assert limiter._owns_client is False, (
        "the limiter claims ownership of the process-wide stand-in; close() "
        "would take sessions, token revocation, dedup and idempotency with it"
    )


@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "/healthcheck",
        "/healthz",
        "/health-status",
        "/readinessz",
        "/readiness-probe",
    ],
)
async def test_a_path_that_merely_starts_with_a_probe_path_is_still_limited(path):
    """M6: the liveness bypass is an exact match, not a prefix.

    A loose ``startswith("/health")`` would hand any route whose path starts
    with those letters a permanent, unauthenticated rate-limit exemption.
    """
    shared = fakeredis_aio.FakeRedis(decode_responses=True)
    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    statuses = [
        (
            await mw.dispatch(
                _http_request(path=path, app=_app(shared), client=ip), _call_next
            )
        ).status_code
        for _ in range(2)
    ]

    assert statuses == [200, 429], (
        f"{path} escaped rate limiting (statuses {statuses}) — the probe bypass "
        "is matching by prefix"
    )


class _SlowToRefuseClient:
    """A dead client whose *ping* takes as long as a real dead pool's does.

    A pool that is gone does not refuse instantly: the re-entry ping runs to
    ``socket_connect_timeout`` (5s) or ``socket_timeout`` (10s). That duration,
    not a request count, is what bounds the re-entry window.
    """

    def __init__(self):
        self.ping_may_return = asyncio.Event()

    async def ping(self):
        await self.ping_may_return.wait()
        raise ConnectionError("connection reset by peer")

    def register_script(self, script):
        async def _run(keys=None, args=None):
            raise ConnectionError("connection reset by peer")

        return _run


@pytest.mark.asyncio
@pytest.mark.unit
async def test_concurrent_arrivals_wait_for_an_in_flight_re_entry(monkeypatch):
    """The re-entry window is bounded by the ping, so it must not be a free-for-all.

    Every request arriving while the first re-entering request awaits its ping
    used to see ``_initialized == False``, fall through to
    ``_serve_without_a_limiter`` and pass unlimited — so the hole was as wide as
    the ping (seconds against a dead pool), not one request wide. Concurrent
    arrivals now wait for the attempt and get a real verdict from it.
    """
    dead = _SlowToRefuseClient()

    async def _factory(redis_url=None):
        from faultmaven.infrastructure.redis_client import get_fakeredis_client

        return get_fakeredis_client()

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    threshold = rate_limiter_module.CHECK_FAILURE_DEMOTION_THRESHOLD
    settings = _limited_settings(global_requests=1)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    ip = _unique_client()

    await mw._initialize(_http_request(app=_app(dead), client=ip))
    await _drive(mw, ip, threshold, state_client=dead)
    assert mw.rate_limiter.demotion_generation == 1, "the client was not declared dead"

    # A burst arrives while the ladder is being re-entered.
    burst = [
        asyncio.create_task(
            mw.dispatch(_http_request(app=_app(dead), client=ip), _call_next)
        )
        for _ in range(30)
    ]
    for _ in range(10):
        await asyncio.sleep(0)
    assert all(not task.done() for task in burst), (
        "requests were answered while the re-entry ping was still outstanding — "
        "they passed unlimited"
    )

    dead.ping_may_return.set()
    statuses = [response.status_code for response in await asyncio.gather(*burst)]

    assert mw._degraded is True, "the ladder was not re-entered"
    assert statuses.count(200) == 1, (
        f"{statuses.count(200)} of {len(statuses)} requests passed during the "
        "re-entry window; the global limit is 1"
    )


# --------------------------------------------------------------------------- #
# The limit the window actually enforces (fm#920)
#
# Every enforcement test above uses ``global_requests=1``, the one limit value
# where counting requests and counting distinct seconds are indistinguishable.
# See docs/architecture/security/rate-limiting-sliding-window.md.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.unit
async def test_the_global_limit_refuses_past_the_configured_request_count(monkeypatch):
    """Five rapid requests from one IP against a limit of 3: three pass, two 429."""
    shared = fakeredis_aio.FakeRedis(decode_responses=True)

    async def _never(redis_url=None):
        raise AssertionError("the shared client from app.state was not adopted")

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _never)

    mw = RateLimitMiddleware(
        app=_asgi_app, settings=_limited_settings(global_requests=3)
    )

    statuses = await _drive(mw, _unique_client(), 5, state_client=shared)

    assert statuses == [200, 200, 200, 429, 429], statuses
