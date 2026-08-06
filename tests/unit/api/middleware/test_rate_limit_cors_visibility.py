"""Limit responses must be readable by the browser that provoked them (fm#930).

CORS used to be registered *first* in ``setup_middleware``, and Starlette wraps
in reverse registration order — so CORS was the innermost layer and only ever
saw responses the route itself produced. Every response the rate limiter
synthesizes is produced *above* it: the 429 short-circuit, the
``_serve_without_a_limiter`` 503 and the dispatch catch-all 503. None of them
carried ``Access-Control-Allow-Origin``, so a cross-origin caller (the Copilot
extension, the Dashboard) got an opaque network error instead of "you are being
rate limited" — and a tripped limit also refused the *preflight*, so the request
that would have reported the limit was never sent at all.

These tests drive a real CORS middleware over a real ``RateLimitMiddleware`` in
the relative order ``setup_middleware`` now produces, and assert at the surface
a browser actually reads: the response headers.
"""

import fakeredis.aioredis as fakeredis_aio
import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.models.protection import RateLimitConfig

pytestmark = [pytest.mark.unit, pytest.mark.security]

_GET_ASYNC_CLIENT = "faultmaven.infrastructure.redis_client.get_async_redis_client"

ORIGIN = "https://app.example.com"
LIMITED_PATH = "/api/v1/cases"

# The rate-limit family, as the browser must be able to read it. Mirrors the
# ``cors_expose_headers`` default — a header the server sets but does not expose
# is invisible to JS, which is the same as not setting it.
RATE_LIMIT_HEADERS = (
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
)


@pytest.fixture(autouse=True)
def _isolated_fakeredis():
    """Each test gets its own stand-in singleton (loop-bound, like the real one)."""
    from faultmaven.infrastructure.redis_client import reset_fakeredis_client

    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


def _settings(*, global_requests=1, fail_open=True):
    settings = get_development_protection_settings()
    settings.fail_open_on_redis_error = fail_open
    settings.rate_limits = {
        "global": RateLimitConfig(enabled=True, requests=global_requests, window=60)
    }
    return settings


def _build_app(settings, redis_client=None):
    """A minimal app stacked the way ``setup_middleware`` now stacks the real one.

    Registration order is the assertion: the limiter is added first (inner) and
    CORS last (outer), so CORS wraps everything the limiter can synthesize.
    """

    async def _endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route(LIMITED_PATH, _endpoint)])
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=list(RATE_LIMIT_HEADERS),
    )
    if redis_client is not None:
        app.state.redis_client = redis_client
    return app


def _get(client, **kwargs):
    return client.get(LIMITED_PATH, headers={"Origin": ORIGIN}, **kwargs)


def test_a_429_carries_the_cors_header():
    """The defect itself: a refused request the browser is allowed to read."""
    app = _build_app(
        _settings(global_requests=1),
        redis_client=fakeredis_aio.FakeRedis(decode_responses=True),
    )

    with TestClient(app) as client:
        assert _get(client).status_code == 200
        refused = _get(client)

    assert refused.status_code == 429, "the limit did not trip; the test is vacuous"
    assert refused.headers.get("access-control-allow-origin") == ORIGIN


def test_a_429_exposes_the_headers_it_sets():
    """Allowing the response through is not enough — JS must be able to read it.

    ``Retry-After`` and the ``X-RateLimit-*`` family are the entire payload of a
    429 as far as a well-behaved client is concerned.
    """
    app = _build_app(
        _settings(global_requests=1),
        redis_client=fakeredis_aio.FakeRedis(decode_responses=True),
    )

    with TestClient(app) as client:
        assert _get(client).status_code == 200
        refused = _get(client)

    exposed = {
        h.strip().lower()
        for h in refused.headers.get("access-control-expose-headers", "").split(",")
    }
    assert refused.status_code == 429
    for header in RATE_LIMIT_HEADERS:
        assert header.lower() in exposed, header


def test_a_tripped_limit_does_not_refuse_the_preflight():
    """OPTIONS is answered by CORS before the limiter ever sees the request.

    With CORS innermost, a client that had exhausted its quota got a 429 on the
    *preflight*, so the browser never sent the request that would have told it
    so — the limit made itself unreportable.
    """
    app = _build_app(
        _settings(global_requests=1),
        redis_client=fakeredis_aio.FakeRedis(decode_responses=True),
    )

    with TestClient(app) as client:
        assert _get(client).status_code == 200
        assert _get(client).status_code == 429  # the quota is spent

        preflight = client.options(
            LIMITED_PATH,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )

    assert preflight.status_code == 200, "a tripped limit refused the preflight"
    assert preflight.headers.get("access-control-allow-origin") == ORIGIN


def test_the_fail_closed_503_carries_the_cors_header(monkeypatch):
    """The other short-circuit: no client at all, fail-closed, no route reached."""

    async def _factory(redis_url=None):
        raise ConnectionError("redis down")

    monkeypatch.setattr(_GET_ASYNC_CLIENT, _factory)

    app = _build_app(_settings(fail_open=False))

    with TestClient(app) as client:
        refused = _get(client)

    assert refused.status_code == 503, "the fail-closed rung was not reached"
    assert refused.headers.get("access-control-allow-origin") == ORIGIN


def test_cors_is_the_outermost_middleware_on_the_real_app():
    """The stacking itself, on the app the deployment actually serves.

    Starlette wraps in reverse registration order, so ``user_middleware[0]`` is
    the last-added and therefore outermost layer — verified empirically, not
    assumed. Anything registered after CORS would sit outside it and short-
    circuit past it again.
    """
    from faultmaven.main import app as real_app

    stack = [entry.cls.__name__ for entry in real_app.user_middleware]

    assert stack, "no middleware registered at all"
    assert stack[0] == "CORSMiddleware", stack
    assert stack.count("CORSMiddleware") == 1, f"more than one CORS authority: {stack}"


def test_the_outermost_position_is_read_the_way_starlette_stacks():
    """Pins the direction the assertion above depends on.

    If Starlette ever reversed ``user_middleware``, the check above would keep
    passing while asserting the opposite of what it means. This measures the
    direction rather than trusting it.
    """
    executed = []

    def _recorder(name):
        from starlette.middleware.base import BaseHTTPMiddleware

        class _M(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                executed.append(name)
                return await call_next(request)

        _M.__name__ = name
        return _M

    async def _endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", _endpoint)])
    app.add_middleware(_recorder("inner"))
    app.add_middleware(_recorder("outer"))

    with TestClient(app) as client:
        client.get("/")

    assert executed == ["outer", "inner"], executed
    assert [entry.cls.__name__ for entry in app.user_middleware] == ["outer", "inner"]


class TestTheStructuralGuard:
    """The check that runs in every environment, tested where it can fail.

    A unit test that imports the app and re-asserts the invariant is worthless:
    it passes because the guard already passed at import, so it re-measures the
    guard's input rather than the guard. The tests below drive
    ``_assert_cors_outermost`` over scratch apps built into each failure state.

    The guard exists because the two ordering tests above run on whatever stack
    the *test* environment builds, and that stack skips several registrations —
    so an ordering assertion made there is blind to the one production serves.
    ``setup_middleware`` calls this on its own final stack, whatever that is.
    """

    @staticmethod
    def _guard():
        from faultmaven.main import _assert_cors_outermost

        return _assert_cors_outermost

    @staticmethod
    def _scratch_app():
        from fastapi import FastAPI

        return FastAPI()

    def test_a_correctly_stacked_app_passes(self):
        app = self._scratch_app()
        app.add_middleware(RateLimitMiddleware, settings=_settings())
        app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN])

        self._guard()(app)  # must not raise

    def test_anything_registered_after_cors_trips_it(self):
        """The defect itself: a layer outside CORS short-circuits past it."""
        app = self._scratch_app()
        app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN])
        app.add_middleware(RateLimitMiddleware, settings=_settings())

        with pytest.raises(RuntimeError, match="outermost"):
            self._guard()(app)

    def test_two_cors_layers_trip_it(self):
        """The same defect from the other side: the inner one is dead config."""
        app = self._scratch_app()
        app.add_middleware(CORSMiddleware, allow_origins=["https://other.example"])
        app.add_middleware(RateLimitMiddleware, settings=_settings())
        app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN])

        with pytest.raises(RuntimeError, match="single outermost"):
            self._guard()(app)

    def test_no_cors_at_all_trips_it(self):
        """An empty stack must not sneak past on an index error."""
        app = self._scratch_app()

        with pytest.raises(RuntimeError, match="outermost"):
            self._guard()(app)

    def test_the_failure_names_the_stack_it_found(self):
        """An ordering failure is unreadable without the order it saw."""
        app = self._scratch_app()
        app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN])
        app.add_middleware(RateLimitMiddleware, settings=_settings())

        with pytest.raises(RuntimeError) as excinfo:
            self._guard()(app)

        assert "RateLimitMiddleware" in str(excinfo.value)
        assert "CORSMiddleware" in str(excinfo.value)

    def test_the_guard_is_not_an_assert(self):
        """``assert`` vanishes under ``-O``, and this must hold in production."""
        import inspect

        source = inspect.getsource(self._guard())

        assert "raise RuntimeError" in source
        assert "assert " not in source, "a stripped guard is not a guard"


def test_the_rate_limit_headers_are_exposed_by_default():
    """The setting the fix above makes reachable.

    Making a 429 traverse CORS is only half of it: a header that is not in
    ``cors_expose_headers`` cannot be read by cross-origin JS, so the caller
    would see the status code and nothing it needs to act on it.
    """
    from faultmaven.config.settings import SecuritySettings

    exposed = set(SecuritySettings(_env_file=None).cors_expose_headers)

    for header in RATE_LIMIT_HEADERS:
        assert header in exposed, header
