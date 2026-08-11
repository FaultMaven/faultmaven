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
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.models.protection import LimitType, RateLimitConfig, RateLimitResult

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


def _request(app, client, headers=None, method="GET"):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
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


async def test_a_429_carries_the_reset_instant_the_limiter_measured():
    """The header is the limiter's own measurement, not a re-derivation.

    ``int(time.time() + retry_after)`` looked equivalent and was not: it reads
    the clock a second time — after the check, the raise and the response
    construction — so the timestamp disagreed with the wait beside it by however
    long that took, and disagreed with the ``reset_time`` the same limiter had
    just put on the served responses. Driven from a controlled result so the
    assertion is exact rather than a tolerance wide enough to hide the defect.
    """
    mw = _middleware(_settings(global_requests=7))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    measured = datetime(2033, 5, 18, 3, 33, 20, tzinfo=timezone.utc)

    async def _refuse(specs):
        return [
            RateLimitResult(
                allowed=False,
                limit_type=LimitType.GLOBAL,
                current_count=7,
                limit=7,
                retry_after=41,
                reset_time=measured,
            )
        ]

    mw.rate_limiter.check_rate_limits = _refuse

    refused = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert refused.status_code == 429
    assert refused.headers["X-RateLimit-Limit"] == "7"
    assert refused.headers["X-RateLimit-Remaining"] == "0"
    assert refused.headers["Retry-After"] == "41"
    assert refused.headers["X-RateLimit-Reset"] == str(int(measured.timestamp()))


async def test_a_429_omits_the_reset_header_when_nothing_measured_one():
    """An unmeasured value is absent, never invented.

    A client that reads no reset instant backs off on its own policy, which is
    strictly better than planning against a fabricated one.
    """
    mw = _middleware(_settings(global_requests=7))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))

    async def _refuse(specs):
        return [
            RateLimitResult(
                allowed=False,
                limit_type=LimitType.GLOBAL,
                current_count=7,
                limit=7,
                retry_after=41,
                reset_time=None,
            )
        ]

    mw.rate_limiter.check_rate_limits = _refuse

    refused = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "41"
    assert "X-RateLimit-Reset" not in refused.headers


async def test_a_429_reset_and_retry_after_name_one_instant_end_to_end():
    """Through the real limiter, the two must still reconcile."""
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


async def test_an_unmeasured_wait_is_absent_not_defaulted():
    """The raise sites pass the limiter's value through; nothing substitutes.

    They used to carry ``or 60`` / ``or 3600``. Those were unreachable — a
    blocked result always carries ``max(1, ceil(...))`` — but unreachable is
    exactly why removing them needed a test that can *see* the difference: a
    default only fires on a falsy value, so every test driving a real refusal
    stays green either way. Driving a blocked result with no wait at all is the
    one input that separates "passed through" from "defaulted", and it also pins
    the rendering: an absent wait is an absent header, never ``str(None)``
    putting the literal text "None" where a duration belongs.
    """
    mw = _middleware(_settings(global_requests=7))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    captured = []

    async def _refuse(specs):
        return [
            RateLimitResult(
                allowed=False,
                limit_type=LimitType.GLOBAL,
                current_count=7,
                limit=7,
                retry_after=None,
                reset_time=None,
            )
        ]

    mw.rate_limiter.check_rate_limits = _refuse

    original = mw._create_rate_limit_response

    def _capture(error, request):
        captured.append(error)
        return original(error, request)

    mw._create_rate_limit_response = _capture

    refused = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert refused.status_code == 429
    assert captured, "the refusal never reached the response builder"
    assert captured[0].retry_after is None, (
        "a fallback substituted a fabricated wait for the one the limiter did "
        f"not measure: {captured[0].retry_after}"
    )
    assert "Retry-After" not in refused.headers, dict(refused.headers)
    assert refused.headers["X-RateLimit-Limit"] == "7"

    # The body is as much the wire as the header is. Omitting the header while
    # the message still read "Retry after None seconds." would hand the client
    # the same garbage one layer down, and a client parsing the body rather
    # than the headers would be no better off than before the omission.
    body = json.loads(refused.body)
    assert "None" not in body["message"], body["message"]
    assert body["retry_after"] is None, body


_ALL_SITES = [
    (LimitType.GLOBAL, "GET"),
    (LimitType.PER_SESSION, "POST"),
    (LimitType.PER_SESSION_HOURLY, "POST"),
    (LimitType.PER_SESSION_READ, "GET"),
    (LimitType.PER_SESSION_READ_HOURLY, "GET"),
]


@pytest.mark.parametrize("refusing_type, method", _ALL_SITES)
async def test_no_raise_site_defaults_an_unmeasured_wait(refusing_type, method):
    """The contract above is a property of every raise site, not of one of them.

    The test beside this one drives a ``global`` refusal, and for a long time
    that was the whole guard — so restoring ``or 60`` / ``or 3600`` on either
    *session* raise site turned nothing red, and the read/write split (fm#994)
    then routed four limit types through those same two sites. A limit's own
    fallback is only reachable through the site that raises for it, so the
    sweep has to name each one: the method chosen per case is what decides
    which pair ``_check_rate_limits`` selects.
    """
    settings = get_development_protection_settings()
    settings.rate_limits = {
        LimitType.GLOBAL.value: RateLimitConfig(enabled=True, requests=7, window=60),
        LimitType.PER_SESSION.value: RateLimitConfig(
            enabled=True, requests=7, window=60
        ),
        LimitType.PER_SESSION_HOURLY.value: RateLimitConfig(
            enabled=True, requests=7, window=3600
        ),
        LimitType.PER_SESSION_READ.value: RateLimitConfig(
            enabled=True, requests=7, window=60
        ),
        LimitType.PER_SESSION_READ_HOURLY.value: RateLimitConfig(
            enabled=True, requests=7, window=3600
        ),
    }

    mw = _middleware(settings)
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    captured = []

    async def _refuse_one(specs):
        # Only the limit type under test refuses, and it refuses with nothing
        # measured — every other window must admit, or the request would be
        # refused by a limit this case is not about.
        return [
            (
                RateLimitResult(
                    allowed=False,
                    limit_type=spec.limit_type,
                    current_count=7,
                    limit=7,
                    retry_after=None,
                    reset_time=None,
                )
                if spec.limit_type is refusing_type
                else RateLimitResult(
                    allowed=True,
                    limit_type=spec.limit_type,
                    current_count=1,
                    limit=7,
                )
            )
            for spec in specs
        ]

    mw.rate_limiter.check_rate_limits = _refuse_one

    original = mw._create_rate_limit_response

    def _capture(error, request):
        captured.append(error)
        return original(error, request)

    mw._create_rate_limit_response = _capture

    request = _request(
        app, _unique_client(), headers={"X-Session-ID": "s-1"}, method=method
    )
    refused = await mw.dispatch(request, _call_next)

    assert refused.status_code == 429, f"{refusing_type} never refused the request"
    assert captured, "the refusal never reached the response builder"
    assert captured[0].limit_type == refusing_type.value, (
        "a different site refused than the one under test — this case is not "
        f"exercising {refusing_type}: {captured[0].limit_type}"
    )
    assert captured[0].retry_after is None, (
        f"the {refusing_type} raise site substituted a fabricated wait for the "
        f"one the limiter did not measure: {captured[0].retry_after}"
    )
    assert "Retry-After" not in refused.headers, dict(refused.headers)


async def test_the_measured_wait_reaches_the_header_however_small():
    """No floor is defaulted in on the way out.

    The raise sites carried ``or 60`` / ``or 3600`` fallbacks. They were
    unreachable — a blocked result always carries ``max(1, ceil(...))`` — but
    had a limiter regression ever made one reachable it would have quietly
    resurrected the flat-window answer on the one response a client acts on.
    A one-second wait must render as one second.
    """
    mw = _middleware(_settings(global_requests=3))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))

    async def _refuse(specs):
        return [
            RateLimitResult(
                allowed=False,
                limit_type=LimitType.GLOBAL,
                current_count=3,
                limit=3,
                retry_after=1,
                reset_time=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
        ]

    mw.rate_limiter.check_rate_limits = _refuse

    refused = await mw.dispatch(_request(app, _unique_client()), _call_next)

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "1", refused.headers["Retry-After"]


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


async def test_the_advertised_numbers_name_the_bucket_they_describe():
    """fm#985 item 11: three numbers with no policy identity are uninterpretable.

    Five buckets can produce the same ``Limit``/``Remaining`` pair, so a client
    that cannot tell an hourly session bucket from a per-minute global one
    cannot pace itself against either. The header names which one the numbers
    came from, in the same token the configuration uses.
    """
    settings = _settings(global_requests=1000, session_requests=4)
    mw = _middleware(settings)
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))

    request = _request(app, _unique_client(), headers={"X-Session-ID": "s-policy"})
    served = await mw.dispatch(request, _call_next)

    assert served.status_code == 200
    # The session bucket (4) is far tighter than the global one (1000), so it is
    # the one advertised — and the policy header must say so rather than leaving
    # the client to guess which of the two it is reading.
    assert served.headers["X-RateLimit-Limit"] == "4"
    assert served.headers["X-RateLimit-Policy"] == LimitType.PER_SESSION.value


async def test_a_refusal_names_the_bucket_that_refused():
    """The same identity on the response a client actually acts on."""
    mw = _middleware(_settings(global_requests=1))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    ip = _unique_client()

    assert (await mw.dispatch(_request(app, ip), _call_next)).status_code == 200
    refused = await mw.dispatch(_request(app, ip), _call_next)

    assert refused.status_code == 429
    assert refused.headers["X-RateLimit-Policy"] == LimitType.GLOBAL.value

    # And the header is the ONLY place that identity reaches the client: the
    # body names counts and a wait but no bucket, so without this header a
    # refused client cannot tell which of five windows turned it away. Pinned
    # so that a future body change does not quietly make the header redundant
    # without anyone noticing it was load-bearing.
    body = json.loads(bytes(refused.body).decode())
    assert LimitType.GLOBAL.value not in json.dumps(body), (
        "the body now carries the bucket identity too — reconcile the two "
        f"rather than leaving them to drift: {body}"
    )


async def test_the_policy_header_is_exposed_to_browser_clients():
    """A header a browser cannot read is a header that was not sent.

    The Copilot and Dashboard are the clients that pace themselves against these
    numbers, and CORS hides any response header not named in ``expose_headers``.
    Advertising the policy without exposing it would repeat exactly the defect
    the other three headers were exposed to fix.
    """
    from faultmaven.config.settings import SecuritySettings

    assert "X-RateLimit-Policy" in SecuritySettings().cors_expose_headers


async def test_all_windows_are_decided_in_one_round_trip():
    """fm#985 item 13: three serial round trips per authenticated request.

    The windows are now one atomic script call, which is also what makes a
    refusal consume no quota (item 8) — the two are the same change. Counted at
    the script boundary rather than timed, so the assertion cannot pass by
    being slow.
    """
    settings = _settings(global_requests=1000, session_requests=50)
    mw = _middleware(settings)
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))

    # Force initialization so the count covers the request path only.
    await mw.dispatch(
        _request(app, _unique_client(), headers={"X-Session-ID": "warm"}), _call_next
    )

    calls = []
    real_script = mw.rate_limiter._window_script

    async def _counting_script(*args, **kwargs):
        calls.append(kwargs.get("keys"))
        return await real_script(*args, **kwargs)

    mw.rate_limiter._window_script = _counting_script

    request = _request(app, _unique_client(), headers={"X-Session-ID": "s-rtt"})
    served = await mw.dispatch(request, _call_next)

    assert served.status_code == 200
    assert len(calls) == 1, f"expected one round trip, got {len(calls)}: {calls}"
    # And it really did decide all three windows, rather than one cheaply.
    assert len(calls[0]) == 3, calls[0]


async def test_a_route_that_poisons_the_inbox_does_not_run_twice():
    """The inbox is a shared namespace, and dispatch's catch-all re-runs the route.

    ``request.state.rate_limit_results`` is written by inner enforcers, so
    anything in the stack can put anything in it. Reading it inside dispatch's
    ``try`` body — rather than inside the header helper's own ``except`` — meant
    an unexpected shape raised into the handler that calls ``call_next`` a
    second time, applying every route side effect twice. Failing to advertise a
    header is a cosmetic loss; running a POST handler twice is not.
    """
    from starlette.responses import JSONResponse

    mw = _middleware(_settings(global_requests=1000))
    app = _app(fakeredis_aio.FakeRedis(decode_responses=True))
    calls = []

    async def _poisoning_call_next(request):
        calls.append(1)
        request.state.rate_limit_results = object()
        return JSONResponse(status_code=200, content={"ok": True})

    response = await mw.dispatch(_request(app, _unique_client()), _poisoning_call_next)

    assert response.status_code == 200
    assert len(calls) == 1, f"the route ran {len(calls)} times"
