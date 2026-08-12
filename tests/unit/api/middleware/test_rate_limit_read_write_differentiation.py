"""Cheap reads are metered apart from writes, by cost rather than by verb (fm#994).

Normal SPA navigation — loading case details, file lists, conversation history —
exceeded the blanket ``per_session`` limit of 10 requests per minute, because the
middleware applied the same quota to lightweight GETs as to POST turns that
consume LLM compute.

The fix gives a session **two independent pairs of buckets**:

- cheap reads → ``per_session_read`` + ``per_session_read_hourly``
- writes, and reads that run an embedding → ``per_session`` +
  ``per_session_hourly``

Both properties in that sentence are load-bearing, and each is the correction of
a way the first attempt at this fix was wrong:

1. **The pairing.** Exempting reads from only the per-minute bucket left them
   counted in the shared *hourly* one, so a read burst still exhausted the
   session — for up to an hour, and for its POST turns too. That is a worse
   outcome than the 60-second lockout it replaced, so the tests below check the
   hourly bucket and the read-then-write sequence, not just the per-minute one.

2. **Cost, not verb.** ``GET .../report-recommendations`` embeds a query and runs
   a similarity search per call. A verb-only rule handed exactly the requests the
   strict bucket exists to protect the roomiest quota in the system.

The read buckets are a limit, not an exemption: reads are bounded by their own
pair, and by ``global`` on top.
"""

import itertools
import logging
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

# A cheap read and a write that the UI actually issues.
CHEAP_READ_PATH = "/api/v1/cases/case-123/ui"
WRITE_PATH = "/api/v1/cases/case-123/turns"

# Reads that cost what a write costs: each embeds a query and searches the
# vector store. Kept in sync with the middleware's own classification by
# ``test_rate_limit_read_cost_classification.py``.
EXPENSIVE_READ_PATHS = (
    "/api/v1/cases/case-123/report-recommendations",
    "/api/v1/knowledge/documents/doc-9/snippet",
)


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
    session_requests=1000,
    session_hourly_requests=1000,
    read_requests=1000,
    read_hourly_requests=1000,
    omit_read_limits=False,
    omit_read_hourly_only=False,
):
    """Build settings with independently controllable buckets.

    Defaults are deliberately roomy so that each test tightens exactly the one
    bucket it is making a claim about, and a failure names that bucket.
    """
    settings = get_development_protection_settings()
    limits = {
        "global": RateLimitConfig(enabled=True, requests=global_requests, window=60),
        "per_session": RateLimitConfig(
            enabled=True, requests=session_requests, window=60
        ),
        "per_session_hourly": RateLimitConfig(
            enabled=True, requests=session_hourly_requests, window=3600
        ),
        "per_session_read": RateLimitConfig(
            enabled=True, requests=read_requests, window=60
        ),
        "per_session_read_hourly": RateLimitConfig(
            enabled=True, requests=read_hourly_requests, window=3600
        ),
    }
    if omit_read_limits:
        del limits["per_session_read"]
        del limits["per_session_read_hourly"]
    elif omit_read_hourly_only:
        del limits["per_session_read_hourly"]
    settings.rate_limits = limits
    return settings


def _app(redis_client):
    return SimpleNamespace(state=SimpleNamespace(redis_client=redis_client))


def _request(app, client, *, method="GET", path=CHEAP_READ_PATH, session_id="sess-1"):
    """Build a Starlette Request with the given method, path and session."""
    hdrs = [(b"host", b"testserver")]
    if session_id:
        hdrs.append((b"x-session-id", session_id.encode()))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": hdrs,
        "app": app,
        "client": client,
    }
    return Request(scope)


async def _ok_response(request):
    return PlainTextResponse("ok")


def _middleware(settings, redis):
    mw = RateLimitMiddleware(_ok_response, settings)
    return mw, _app(redis), _unique_client()


# ---------------------------------------------------------------------------
# The split: cheap reads do not touch the write buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cheap_reads_do_not_consume_the_write_per_minute_bucket():
    mw, app, client = _middleware(
        _settings(session_requests=2), fakeredis_aio.FakeRedis()
    )

    for i in range(10):
        resp = await mw.dispatch(_request(app, client), _ok_response)
        assert resp.status_code == 200, f"read #{i + 1} charged to per_session"


@pytest.mark.asyncio
async def test_cheap_reads_do_not_consume_the_write_hourly_bucket():
    """The regression the first fix left behind.

    Exempting reads from the per-minute bucket alone still charged them to the
    shared hourly one, so a read burst locked the session out for up to an hour.
    """
    mw, app, client = _middleware(
        _settings(session_hourly_requests=2), fakeredis_aio.FakeRedis()
    )

    for i in range(10):
        resp = await mw.dispatch(_request(app, client), _ok_response)
        assert resp.status_code == 200, f"read #{i + 1} charged to per_session_hourly"


@pytest.mark.asyncio
async def test_a_read_burst_does_not_refuse_the_next_write():
    """The property the user reported, stated end to end.

    Both write buckets are set to exactly one request, so if a read charged
    either of them the POST that follows would be refused.
    """
    mw, app, client = _middleware(
        _settings(session_requests=1, session_hourly_requests=1),
        fakeredis_aio.FakeRedis(),
    )

    for _ in range(50):
        resp = await mw.dispatch(_request(app, client), _ok_response)
        assert resp.status_code == 200

    resp = await mw.dispatch(
        _request(app, client, method="POST", path=WRITE_PATH), _ok_response
    )
    assert resp.status_code == 200, "a read burst consumed the turn quota"


# ---------------------------------------------------------------------------
# The split is a limit, not an exemption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cheap_reads_are_bounded_by_the_read_per_minute_bucket():
    mw, app, client = _middleware(_settings(read_requests=3), fakeredis_aio.FakeRedis())

    for _ in range(3):
        assert (
            await mw.dispatch(_request(app, client), _ok_response)
        ).status_code == 200

    resp = await mw.dispatch(_request(app, client), _ok_response)
    assert resp.status_code == 429, "reads are not bounded by per_session_read"
    assert resp.headers["X-RateLimit-Limit"] == "3"
    assert (
        int(resp.headers["Retry-After"]) <= 60
    ), "a per-minute read refusal must not hand back an hour-long wait"


@pytest.mark.asyncio
async def test_cheap_reads_are_bounded_by_the_read_hourly_bucket():
    mw, app, client = _middleware(
        _settings(read_hourly_requests=3), fakeredis_aio.FakeRedis()
    )

    for _ in range(3):
        assert (
            await mw.dispatch(_request(app, client), _ok_response)
        ).status_code == 200

    resp = await mw.dispatch(_request(app, client), _ok_response)
    assert resp.status_code == 429, "reads are not bounded by per_session_read_hourly"
    assert resp.headers["X-RateLimit-Limit"] == "3"


@pytest.mark.asyncio
async def test_cheap_reads_are_bounded_by_the_global_limit():
    mw, app, client = _middleware(
        _settings(global_requests=3), fakeredis_aio.FakeRedis()
    )

    for _ in range(3):
        assert (
            await mw.dispatch(_request(app, client), _ok_response)
        ).status_code == 200

    resp = await mw.dispatch(_request(app, client), _ok_response)
    assert resp.status_code == 429, "reads escaped the global limit"


@pytest.mark.asyncio
async def test_exhausting_the_read_bucket_leaves_writes_alone():
    """The converse of the burst test: refusing reads must not refuse turns."""
    mw, app, client = _middleware(_settings(read_requests=2), fakeredis_aio.FakeRedis())

    for _ in range(2):
        await mw.dispatch(_request(app, client), _ok_response)
    assert (await mw.dispatch(_request(app, client), _ok_response)).status_code == 429

    resp = await mw.dispatch(
        _request(app, client, method="POST", path=WRITE_PATH), _ok_response
    )
    assert resp.status_code == 200, "a read lockout spilled onto writes"


# ---------------------------------------------------------------------------
# Writes stay on the strict buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_consume_the_write_per_minute_bucket():
    mw, app, client = _middleware(
        _settings(session_requests=2), fakeredis_aio.FakeRedis()
    )

    for i in range(2):
        resp = await mw.dispatch(
            _request(app, client, method="POST", path=WRITE_PATH), _ok_response
        )
        assert resp.status_code == 200, f"POST #{i + 1} should succeed"

    resp = await mw.dispatch(
        _request(app, client, method="POST", path=WRITE_PATH), _ok_response
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_other_write_verbs_consume_the_write_bucket(method):
    mw, app, client = _middleware(
        _settings(session_requests=1), fakeredis_aio.FakeRedis()
    )
    session = f"sess-{method}"

    resp = await mw.dispatch(
        _request(app, client, method=method, path=WRITE_PATH, session_id=session),
        _ok_response,
    )
    assert resp.status_code == 200, f"first {method} should succeed"

    resp = await mw.dispatch(
        _request(app, client, method=method, path=WRITE_PATH, session_id=session),
        _ok_response,
    )
    assert resp.status_code == 429, f"second {method} should be refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
async def test_head_and_options_are_cheap_reads(method):
    mw, app, client = _middleware(
        _settings(session_requests=1), fakeredis_aio.FakeRedis()
    )

    for _ in range(5):
        resp = await mw.dispatch(_request(app, client, method=method), _ok_response)
        assert resp.status_code == 200, f"{method} charged to per_session"


# ---------------------------------------------------------------------------
# Cost, not verb: embedding-backed GETs stay on the strict buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", EXPENSIVE_READ_PATHS)
async def test_expensive_reads_consume_the_write_bucket(path):
    """These GETs run a query embedding and a vector search per call.

    Metering them as cheap reads raised their per-session ceiling from the
    strict bucket to the roomiest limit in the system.
    """
    mw, app, client = _middleware(
        _settings(session_requests=2), fakeredis_aio.FakeRedis()
    )

    for _ in range(2):
        resp = await mw.dispatch(_request(app, client, path=path), _ok_response)
        assert resp.status_code == 200

    resp = await mw.dispatch(_request(app, client, path=path), _ok_response)
    assert resp.status_code == 429, f"{path} was metered as a cheap read"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", EXPENSIVE_READ_PATHS)
async def test_expensive_reads_do_not_consume_the_read_bucket(path):
    """A request is charged to one pair of buckets, not to both."""
    mw, app, client = _middleware(_settings(read_requests=1), fakeredis_aio.FakeRedis())

    for _ in range(5):
        resp = await mw.dispatch(_request(app, client, path=path), _ok_response)
        assert resp.status_code == 200, f"{path} was also charged to per_session_read"


@pytest.mark.asyncio
async def test_sibling_paths_of_an_expensive_read_stay_cheap():
    """The patterns are anchored, so neither classification leaks by prefix."""
    mw, app, client = _middleware(
        _settings(session_requests=1), fakeredis_aio.FakeRedis()
    )

    neighbours = (
        "/api/v1/cases/case-123/report-recommendations/history",
        "/api/v1/cases/case-123/reports",
        "/api/v1/knowledge/documents/doc-9",
    )
    for path in neighbours:
        for _ in range(3):
            resp = await mw.dispatch(_request(app, client, path=path), _ok_response)
            assert resp.status_code == 200, f"{path} was misread as expensive"


# ---------------------------------------------------------------------------
# A missing read bucket narrows; it never widens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_read_buckets_fall_back_to_the_write_buckets():
    """``check_rate_limit`` allows anything it holds no config for.

    So routing reads to an unconfigured bucket would not meter them at all. A
    settings object written before the split must lose the roomier quota, not
    gain an unlimited one.
    """
    mw, app, client = _middleware(
        _settings(session_requests=2, omit_read_limits=True), fakeredis_aio.FakeRedis()
    )

    for _ in range(2):
        assert (
            await mw.dispatch(_request(app, client), _ok_response)
        ).status_code == 200

    resp = await mw.dispatch(_request(app, client), _ok_response)
    assert (
        resp.status_code == 429
    ), "reads went unmetered with no read bucket configured"


@pytest.mark.asyncio
async def test_a_half_configured_read_split_is_refused(caplog):
    """The per-minute read bucket without its hourly partner is the worst shape.

    It would cap a read flood at "whatever ``global`` allows" for a whole hour,
    so the split is refused outright rather than half-applied.
    """
    with caplog.at_level(logging.ERROR):
        mw, app, client = _middleware(
            _settings(session_hourly_requests=2, omit_read_hourly_only=True),
            fakeredis_aio.FakeRedis(),
        )

    assert any(
        "partial read-bucket configuration" in record.message
        for record in caplog.records
    ), "a half-configured split was applied silently"

    for _ in range(2):
        assert (
            await mw.dispatch(_request(app, client), _ok_response)
        ).status_code == 200

    resp = await mw.dispatch(_request(app, client), _ok_response)
    assert resp.status_code == 429, "reads escaped both hourly buckets"


# ---------------------------------------------------------------------------
# The shipped presets carry both pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loader",
    [
        "get_development_protection_settings",
        "get_production_protection_settings",
    ],
)
def test_every_loader_configures_both_pairs(loader):
    """A preset that omits the read keys silently reinstates fm#994."""
    import faultmaven.config.protection as protection_config

    settings = getattr(protection_config, loader)()

    for key in (
        "per_session",
        "per_session_hourly",
        "per_session_read",
        "per_session_read_hourly",
    ):
        assert key in settings.rate_limits, f"{loader} omits {key}"

    assert (
        settings.rate_limits["per_session_read"].requests
        > settings.rate_limits["per_session"].requests
    ), f"{loader} gives reads no more room than writes"
    assert (
        settings.rate_limits["per_session_read_hourly"].requests
        > settings.rate_limits["per_session_hourly"].requests
    ), f"{loader} gives reads no more hourly room than writes"


def test_the_default_settings_model_carries_both_pairs():
    """``ProtectionSettings()`` with no arguments must not be the widening path."""
    from faultmaven.models.protection import ProtectionSettings

    limits = ProtectionSettings().rate_limits
    assert "per_session_read" in limits
    assert "per_session_read_hourly" in limits
