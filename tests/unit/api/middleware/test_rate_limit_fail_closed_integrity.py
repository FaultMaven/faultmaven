"""A Redis failure is an error, not a limit violation (fm#932).

Under ``fail_open_on_redis_error=False`` — production's pin — ``check_rate_limits``
converted any Redis error into ``RateLimitError(retry_after=60, current_count=0,
limit=0)``. The middleware then rendered a 429 reading "0/0 requests", counted it
in ``requests_blocked`` and WARN-logged the caller as a rate-limit violator: a
fabricated accusation against a client that did nothing, up to three times per
client death before the demotion threshold engages.

The correct rung already existed. Re-raising the original error reaches the
dispatch catch-all, which is the 503 rung, the ``errors`` counter and no
violator log — and the exception that reaches the log names the Redis failure
instead of a limit nobody hit.
"""

import itertools
import logging
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.infrastructure.protection.rate_limiter import (
    CHECK_FAILURE_DEMOTION_THRESHOLD,
)
from faultmaven.models.protection import LimitType, RateLimitConfig, RateLimitSpec

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
    return (f"10.4.0.{next(_IP_COUNTER)}", 1234)


class _FailingClient:
    """A client whose window script raises — a pool that died mid-life."""

    def __init__(self, error=None):
        self.error = error or ConnectionError("connection reset by peer")

    async def ping(self):
        return True

    def register_script(self, script):
        async def _run(keys=None, args=None):
            raise self.error

        return _run

    async def close(self):
        pass


def _settings(*, fail_open):
    settings = get_development_protection_settings()
    settings.fail_open_on_redis_error = fail_open
    settings.rate_limits = {
        "global": RateLimitConfig(enabled=True, requests=5, window=60)
    }
    return settings


def _app(redis_client):
    return SimpleNamespace(state=SimpleNamespace(redis_client=redis_client))


def _request(app, client):
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
        "headers": [],
        "client": client,
        "app": app,
    }
    return Request(scope)


async def _call_next(request):
    return PlainTextResponse("ok")


def _fail_closed_middleware(client=None):
    mw = RateLimitMiddleware(app=_asgi_app, settings=_settings(fail_open=False))
    assert mw.rate_limiter.fallback_enabled is False
    return mw, _app(client or _FailingClient())


async def test_a_dead_client_yields_503_not_a_fabricated_429():
    """The defect itself, at the surface the caller sees."""
    mw, app = _fail_closed_middleware()

    response = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert response.status_code == 503, "a Redis error was rendered as a 429"


async def test_the_failure_counts_as_an_error_not_a_blocked_request():
    """``requests_blocked`` is the rate-limiting signal operators alert on.

    Inflating it with Redis outages makes the alert fire for the wrong reason
    and hides the outage behind a limit that was never reached.
    """
    mw, app = _fail_closed_middleware()

    await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert mw.metrics["requests_blocked"] == 0
    assert mw.metrics["errors"] == 1


async def test_the_client_is_not_logged_as_a_rate_limit_violator(caplog):
    """An accusation in the audit trail against a caller that did nothing."""
    mw, app = _fail_closed_middleware()

    with caplog.at_level(logging.WARNING):
        await mw.dispatch(_request(app, _unique_client()), _call_next)

    messages = [record.getMessage() for record in caplog.records]
    assert not [m for m in messages if "Rate limit exceeded" in m], messages


async def test_the_underlying_cause_survives_into_the_log(caplog):
    """A manufactured RateLimitError discarded the exception that caused it."""
    mw, app = _fail_closed_middleware(_FailingClient(TimeoutError("redis timed out")))

    with caplog.at_level(logging.ERROR):
        await mw.dispatch(_request(app, _unique_client()), _call_next)

    messages = [record.getMessage() for record in caplog.records]
    assert any("TimeoutError" in m for m in messages), messages


async def test_repeated_failures_still_demote_the_client():
    """Re-raising must not cost the liveness tracking that sits above it.

    ``_record_check_failure`` runs before the branch, so a run of failures still
    declares the client dead and bumps the generation the middleware watches.
    """
    mw, app = _fail_closed_middleware()
    before = mw.rate_limiter.demotion_generation

    for _ in range(CHECK_FAILURE_DEMOTION_THRESHOLD):
        response = await mw.dispatch(_request(app, _unique_client()), _call_next)
        assert response.status_code == 503

    assert mw.rate_limiter.demotion_generation == before + 1


async def test_fail_open_is_unchanged():
    """The other branch is not touched: a blip still passes the request.

    Without this the fix could be "everything 503s now", which is a different
    and worse bug.
    """
    settings = _settings(fail_open=True)
    mw = RateLimitMiddleware(app=_asgi_app, settings=settings)
    app = _app(_FailingClient())

    response = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert response.status_code == 200
    assert mw.metrics["requests_blocked"] == 0


async def test_the_limiter_re_raises_the_original_exception():
    """The contract the middleware's rung selection depends on."""
    from faultmaven.infrastructure.protection.rate_limiter import RedisRateLimiter

    limiter = RedisRateLimiter(fallback_enabled=False)
    boom = ConnectionError("connection reset by peer")
    await limiter._adopt(_FailingClient(boom), owns=False, degraded=False)
    limiter.configure_limits(
        {LimitType.GLOBAL.value: RateLimitConfig(enabled=True, requests=5, window=60)}
    )

    with pytest.raises(ConnectionError) as raised:
        await limiter.check_rate_limits(
            [RateLimitSpec(key="10.4.9.1", limit_type=LimitType.GLOBAL)]
        )

    assert raised.value is boom
