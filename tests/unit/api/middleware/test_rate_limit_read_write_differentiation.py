"""Read-only requests skip the tight per-session per-minute limit (fm#994).

Normal SPA navigation — loading case details, file lists, conversation history —
easily exceeded the previous blanket ``per_session`` limit of 10 requests per
minute, because the middleware applied the same quota to lightweight GETs as to
heavy POST turns that consume LLM compute.

The fix exempts GET / HEAD / OPTIONS from the ``per_session`` per-minute bucket.
Read-only traffic is still bounded by the ``global`` limit and the
``per_session_hourly`` limit, so it is not unlimited.

These tests verify the exemption boundary:

1. GETs do not consume per_session per-minute quota.
2. POSTs *do* consume per_session per-minute quota and are blocked when
   exhausted.
3. GETs keep working after POSTs exhaust per_session.
4. GETs are still subject to per_session_hourly.
5. GETs are still subject to the global limit.
"""

import itertools
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.models.protection import RateLimitConfig, RateLimitError

pytestmark = [pytest.mark.unit, pytest.mark.security]

_IP_COUNTER = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolated_fakeredis():
    from faultmaven.infrastructure.redis_client import reset_fakeredis_client

    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


def _unique_client():
    """A fresh client IP per test — the global limit is keyed on it."""
    return (f"10.4.0.{next(_IP_COUNTER)}", 1234)


def _settings(
    *,
    global_requests=1000,
    session_requests=3,
    session_hourly_requests=100,
):
    """Build settings with controllable limits."""
    settings = get_development_protection_settings()
    settings.rate_limits = {
        "global": RateLimitConfig(enabled=True, requests=global_requests, window=60),
        "per_session": RateLimitConfig(
            enabled=True, requests=session_requests, window=60
        ),
        "per_session_hourly": RateLimitConfig(
            enabled=True, requests=session_hourly_requests, window=3600
        ),
    }
    return settings


def _app(redis_client):
    return SimpleNamespace(state=SimpleNamespace(redis_client=redis_client))


def _request(app, client, *, method="GET", headers=None, session_id="sess-1"):
    """Build a Starlette Request with the given method and optional session."""
    hdrs = [(b"host", b"testserver")]
    if session_id:
        hdrs.append((b"x-session-id", session_id.encode()))
    if headers:
        for k, v in headers.items():
            hdrs.append((k.encode(), v.encode()))

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
        "headers": hdrs,
        "app": app,
        "client": client,
    }
    return Request(scope)


async def _ok_response(request):
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_requests_do_not_consume_per_session_per_minute():
    """GETs should not be counted against the per-session per-minute limit."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(session_requests=2)  # Only 2 writes allowed per minute
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)
    client = _unique_client()

    # Fire 10 GETs — all should succeed despite per_session=2
    for _ in range(10):
        resp = await mw.dispatch(_request(app, client, method="GET"), _ok_response)
        assert resp.status_code == 200, "GET should not be rate-limited by per_session"


@pytest.mark.asyncio
async def test_post_requests_consume_per_session_per_minute():
    """POSTs should be counted against the per-session per-minute limit."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(session_requests=2)
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)
    client = _unique_client()

    # First 2 POSTs succeed
    for i in range(2):
        resp = await mw.dispatch(_request(app, client, method="POST"), _ok_response)
        assert resp.status_code == 200, f"POST #{i+1} should succeed"

    # 3rd POST should be blocked
    resp = await mw.dispatch(_request(app, client, method="POST"), _ok_response)
    assert (
        resp.status_code == 429
    ), "POST should be rate-limited after exceeding per_session"


@pytest.mark.asyncio
async def test_get_still_works_after_post_exhausts_per_session():
    """GETs must keep working even when POSTs have exhausted per_session."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(session_requests=2)
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)
    client = _unique_client()

    # Exhaust per_session with POSTs
    for _ in range(2):
        await mw.dispatch(_request(app, client, method="POST"), _ok_response)

    # Verify POST is blocked
    resp = await mw.dispatch(_request(app, client, method="POST"), _ok_response)
    assert resp.status_code == 429

    # GETs should still work
    for _ in range(5):
        resp = await mw.dispatch(_request(app, client, method="GET"), _ok_response)
        assert (
            resp.status_code == 200
        ), "GET should succeed even after per_session exhausted"


@pytest.mark.asyncio
async def test_get_requests_still_subject_to_per_session_hourly():
    """GETs must still be bounded by the per_session_hourly limit."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(session_requests=1000, session_hourly_requests=3)
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)
    client = _unique_client()

    # Fire 3 GETs — should succeed
    for _ in range(3):
        resp = await mw.dispatch(_request(app, client, method="GET"), _ok_response)
        assert resp.status_code == 200

    # 4th GET should be blocked by hourly limit
    resp = await mw.dispatch(_request(app, client, method="GET"), _ok_response)
    assert resp.status_code == 429, "GET should be blocked by per_session_hourly"


@pytest.mark.asyncio
async def test_get_requests_still_subject_to_global_limit():
    """GETs must still be bounded by the global rate limit."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(
        global_requests=3, session_requests=1000, session_hourly_requests=1000
    )
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)
    client = _unique_client()

    # Fire 3 GETs — should succeed
    for _ in range(3):
        resp = await mw.dispatch(_request(app, client, method="GET"), _ok_response)
        assert resp.status_code == 200

    # 4th GET should be blocked by global limit
    resp = await mw.dispatch(_request(app, client, method="GET"), _ok_response)
    assert resp.status_code == 429, "GET should be blocked by global limit"


@pytest.mark.asyncio
async def test_head_and_options_also_exempt_from_per_session():
    """HEAD and OPTIONS should be treated like GET — exempt from per_session."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(session_requests=1)  # Only 1 write per minute
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)
    client = _unique_client()

    for method in ("HEAD", "OPTIONS"):
        for _ in range(5):
            resp = await mw.dispatch(_request(app, client, method=method), _ok_response)
            assert resp.status_code == 200, f"{method} should not consume per_session"


@pytest.mark.asyncio
async def test_put_patch_delete_consume_per_session():
    """PUT, PATCH, DELETE are write operations and should consume per_session."""

    redis = fakeredis_aio.FakeRedis()
    settings = _settings(session_requests=1)
    mw = RateLimitMiddleware(_ok_response, settings)
    app = _app(redis)

    for method in ("PUT", "PATCH", "DELETE"):
        client = _unique_client()  # Fresh IP to avoid global limit overlap
        # First request succeeds
        resp = await mw.dispatch(
            _request(app, client, method=method, session_id=f"sess-{method}"),
            _ok_response,
        )
        assert resp.status_code == 200, f"First {method} should succeed"

        # Second request should be blocked
        resp = await mw.dispatch(
            _request(app, client, method=method, session_id=f"sess-{method}"),
            _ok_response,
        )
        assert resp.status_code == 429, f"Second {method} should be rate-limited"
