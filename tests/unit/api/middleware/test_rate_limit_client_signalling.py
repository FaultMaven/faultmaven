"""The middleware tells every client where it stands (fm#931).

Two holes on the response side. A 429 advertised ``Retry-After`` and
``X-RateLimit-Limit`` but no ``X-RateLimit-Reset``, so a client tracking reset
instants lost the value on exactly the response that mattered. And the 2xx
header path was gated on a session id *and* hardwired to ``PER_SESSION``, so
unauthenticated traffic — the only traffic the ``global`` limit exists for —
was never told anything at all, and an authenticated client was never told
about the global limit it might be hitting first.

The headers now come from the ``RateLimitResult``s the checks already produced,
so nothing here costs an extra Redis round trip.
"""

import itertools
import time
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.models.protection import RateLimitConfig

pytestmark = [pytest.mark.unit, pytest.mark.security]

_IP_COUNTER = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolated_fakeredis():
    from faultmaven.infrastructure.redis_client import reset_fakeredis_client

    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


def _asgi_app(scope, receive, send):
    return None


def _unique_client():
    """A fresh client IP per test — the global limit is keyed on it."""
    return (f"10.3.0.{next(_IP_COUNTER)}", 1234)


def _settings(*, global_requests=2, session_requests=None):
    settings = get_development_protection_settings()
    limits = {
        "global": RateLimitConfig(enabled=True, requests=global_requests, window=60)
    }
    if session_requests is not None:
        limits["per_session"] = RateLimitConfig(
            enabled=True, requests=session_requests, window=60
        )
        limits["per_session_hourly"] = RateLimitConfig(
            enabled=True, requests=10_000, window=3600
        )
    settings.rate_limits = limits
    return settings


def _app(redis_client):
    return SimpleNamespace(state=SimpleNamespace(redis_client=redis_client))


def _request(app, client, headers=None):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
        "path": "/api/v1/cases",
        "raw_path": b"/api/v1/cases",
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": client,
        "app": app,
    }
    return Request(scope)


async def _call_next(request):
    return PlainTextResponse("ok")


def _middleware(settings):
    return RateLimitMiddleware(app=_asgi_app, settings=settings)


async def test_an_unauthenticated_2xx_advertises_the_global_limit():
    """The hole: no session id used to mean no headers at all.

    ``global`` is the only limit covering unauthenticated traffic, so silence
    here left exactly the callers who most need to pace themselves with nothing
    to pace against.
    """
    mw = _middleware(_settings(global_requests=5))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    ip = _unique_client()

    response = await mw.dispatch(_request(app, ip), _call_next)

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "4"
    assert int(response.headers["X-RateLimit-Reset"]) > 0


async def test_remaining_counts_down_across_requests():
    """Not a constant: the advertised quota tracks what the client has spent."""
    mw = _middleware(_settings(global_requests=3))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    ip = _unique_client()

    remaining = []
    for _ in range(3):
        response = await mw.dispatch(_request(app, ip), _call_next)
        remaining.append(response.headers["X-RateLimit-Remaining"])

    assert remaining == ["2", "1", "0"]


async def test_the_reset_instant_does_not_march_with_the_clock():
    """It names when quota frees, so it is stable across a client's window."""
    mw = _middleware(_settings(global_requests=5))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    ip = _unique_client()

    first = await mw.dispatch(_request(app, ip), _call_next)
    second = await mw.dispatch(_request(app, ip), _call_next)

    assert first.headers["X-RateLimit-Reset"] == second.headers["X-RateLimit-Reset"]


async def test_a_429_carries_a_reset_consistent_with_retry_after():
    """The two express the same instant; a client may use either."""
    mw = _middleware(_settings(global_requests=1))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    ip = _unique_client()

    assert (await mw.dispatch(_request(app, ip), _call_next)).status_code == 200
    refused = await mw.dispatch(_request(app, ip), _call_next)

    assert refused.status_code == 429
    retry_after = int(refused.headers["Retry-After"])
    reset = int(refused.headers["X-RateLimit-Reset"])

    assert refused.headers["X-RateLimit-Remaining"] == "0"
    assert refused.headers["X-RateLimit-Limit"] == "1"
    # Same instant, allowing for the second that may tick during the check.
    assert abs(reset - (int(time.time()) + retry_after)) <= 1, (reset, retry_after)


async def test_the_headers_report_the_tightest_limit():
    """A client that respects the tightest limit respects all of them.

    Reporting a roomier one would invite it to speed up into the tighter one.

    Uses POST because fm#994 exempts read-only methods (GET/HEAD/OPTIONS)
    from the per-session per-minute limit, so only a write request produces
    a ``per_session`` result for the header comparison.
    """
    mw = _middleware(_settings(global_requests=100, session_requests=4))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    ip = _unique_client()
    headers = {"X-Session-ID": "session-tightest"}

    # Build a POST request to exercise the per_session check
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
        "path": "/api/v1/cases",
        "raw_path": b"/api/v1/cases",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ip,
        "app": app,
    }
    request = Request(scope)

    response = await mw.dispatch(request, _call_next)

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "4", "reported the roomier limit"
    assert response.headers["X-RateLimit-Remaining"] == "3"


async def test_the_headers_cost_no_extra_redis_round_trip():
    """They are emitted from results the checks already read.

    The old path re-queried ``get_rate_limit_status`` on every served response —
    an extra round trip per request to learn a number already in hand.
    """
    mw = _middleware(_settings(global_requests=5))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("the header path queried Redis again")

    mw.rate_limiter.get_rate_limit_status = _must_not_be_called

    response = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "5"


async def test_a_disabled_limit_is_not_advertised_as_a_limit_of_zero():
    """``limit == 0`` is "no limit configured", and also what a failed-open
    check reports. Publishing it would tell every client it has no quota at all.
    """
    settings = _settings(global_requests=5)
    settings.rate_limits["global"] = RateLimitConfig(
        enabled=False, requests=5, window=60
    )
    mw = _middleware(settings)
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))

    response = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers
    assert "X-RateLimit-Remaining" not in response.headers
