"""A duplicate request must actually be answered as one.

The middleware built a labelled 409 with a ``Retry-After`` and never reached it.
``_check_redis_duplicate`` returned the value stored in Redis -- the *first
request's timestamp*, which is what ``Retry-After`` is computed from -- and
``dispatch`` treated that truthy string as a cached response body::

    if cached_response:
        return JSONResponse(content=json.loads(cached_response))

``json.loads("2026-08-10T01:02:03Z")`` raises, the outer ``except`` swallowed it,
and the duplicate was answered by whichever failure mode was configured: passed
through as a normal 200 under ``fail_open_on_redis_error`` (the default), or an
**unlabelled 503** under the fail-closed setting production pins. The 409 branch
was dead in both.

The cache it was reading never existed: the writer stored responses under
``{key}:response`` while the only read was ``GET {key}``. Response caching is
removed rather than repaired -- ``cache_responses`` was ``False`` everywhere, and
its one configured endpoint (``/api/v1/data/upload``) is multipart, which
deduplication skips outright.

**On writing these tests.** A "not a duplicate" assertion is worth nothing on its
own: a *broken* duplicate check also returns 200, so such a test passes whether
the feature works or not. The first version of this file had three of those, and
they sailed through CI while the two real assertions failed. So every negative
case here first proves deduplication is live in the same test, and the fixture
fails the test if the middleware swallowed anything.

The failure that exposed it is also why the Redis client is built inside the
app's startup rather than at fixture time: ``fakeredis.aioredis`` binds to the
loop it is constructed on, and ``TestClient`` serves on its own.
"""

from contextlib import asynccontextmanager

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.config.protection import get_development_protection_settings

pytestmark = [pytest.mark.unit, pytest.mark.security]

BODY = b'{"query":"why is the db slow"}'


async def _raise_redis_error(*_args, **_kwargs):
    raise RuntimeError("redis is down")


class _Harness:
    """A TestClient plus the middleware instance, so tests can inspect metrics."""

    def __init__(self, client: TestClient, app: FastAPI):
        self.client = client
        self._app = app

    @property
    def middleware(self) -> DeduplicationMiddleware:
        # Starlette builds the stack on startup; find our instance in it.
        stack = self._app.middleware_stack
        while stack is not None:
            if isinstance(stack, DeduplicationMiddleware):
                return stack
            stack = getattr(stack, "app", None)
        raise AssertionError("DeduplicationMiddleware is not in the stack")

    def post(self, body: bytes = BODY, session: str = "sess-1"):
        return self.client.post(
            "/api/v1/agent/query",
            content=body,
            headers={"X-Session-ID": session, "content-type": "application/json"},
        )

    def assert_nothing_was_swallowed(self):
        """No test here may pass because the middleware silently gave up."""
        assert self.middleware.metrics["errors"] == 0


@pytest.fixture
def harness(request):
    """Build an app whose Redis client is created on the serving event loop."""
    fail_open = getattr(request, "param", True)

    settings = get_development_protection_settings()
    settings.deduplication_enabled = True
    settings.fail_open_on_redis_error = fail_open

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Constructed here, not at fixture time: fakeredis.aioredis binds its
        # internal queues to the running loop, and TestClient serves on its own.
        # Built outside it, every Redis call raises "bound to a different event
        # loop" -- which the middleware used to swallow into "not a duplicate".
        app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
        yield

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(DeduplicationMiddleware, settings=settings)

    @app.post("/api/v1/agent/query")
    async def _query():
        return {"ok": True}

    with TestClient(app) as client:
        yield _Harness(client, app)


@pytest.mark.parametrize("harness", [True, False], indirect=True)
def test_duplicate_gets_a_labelled_409(harness):
    """Parametrized over both failure policies.

    The old behaviour diverged by policy -- 200 when failing open, 503 when
    failing closed -- so pinning only one would leave the other free to regress.
    A duplicate is a duplicate under either setting.
    """
    assert harness.post().status_code == 200

    second = harness.post()
    assert second.status_code == 409
    # Unlabelled 409s are read by the Slack agent as "this case is terminal"
    # (see test_conflict_labelling.py) -- this one must carry its code.
    assert second.headers.get("x-error-code")
    assert int(second.headers["Retry-After"]) > 0
    assert second.json()["error_code"] == second.headers["x-error-code"]
    harness.assert_nothing_was_swallowed()


def test_a_different_body_is_not_a_duplicate(harness):
    """The half the old normalizer got wrong, pinned end to end.

    Proves deduplication is live first, so a broken check cannot make this pass.
    """
    assert harness.post(b'{"query":"check order 4232342342"}').status_code == 200
    assert harness.post(b'{"query":"check order 9994442211"}').status_code == 200
    # ... and the second body *is* caught on its own resubmit.
    assert harness.post(b'{"query":"check order 9994442211"}').status_code == 409
    harness.assert_nothing_was_swallowed()


def test_a_different_session_is_not_a_duplicate(harness):
    assert harness.post(session="sess-1").status_code == 200
    assert harness.post(session="sess-2").status_code == 200
    assert harness.post(session="sess-2").status_code == 409
    harness.assert_nothing_was_swallowed()


def test_no_session_id_means_no_deduplication(harness):
    """Dedup keys on the session; without one there is nothing to key on.

    Paired with a session-bearing resubmit so the 200s cannot come from a
    deduplication check that is simply broken.
    """
    for _ in range(2):
        response = harness.client.post(
            "/api/v1/agent/query",
            content=BODY,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200

    assert harness.post().status_code == 200
    assert harness.post().status_code == 409
    harness.assert_nothing_was_swallowed()


@pytest.mark.parametrize("harness", [False], indirect=True)
def test_a_redis_failure_fails_closed(harness, monkeypatch):
    """The fail-closed setting must actually cover the duplicate check.

    `_check_hash_duplicate` used to catch every exception and answer "not a
    duplicate", so `fail_open_on_redis_error=False` -- which production pins --
    did not reach this path at all: a broken Redis quietly admitted every
    duplicate while the policy claimed the opposite. Restoring that `except`
    turns this test green-to-red, which is the point; without it the property
    had no committed guard.
    """
    monkeypatch.setattr(
        harness.middleware,
        "_check_redis_duplicate",
        _raise_redis_error,
    )

    assert harness.post().status_code == 503
    assert harness.middleware.metrics["errors"] == 1


@pytest.mark.parametrize("harness", [True], indirect=True)
def test_a_redis_failure_fails_open_when_configured(harness, monkeypatch):
    """The other half of the policy: fail-open still serves the request."""
    monkeypatch.setattr(
        harness.middleware,
        "_check_redis_duplicate",
        _raise_redis_error,
    )

    assert harness.post().status_code == 200


@pytest.mark.parametrize("reply", [None, []])
def test_an_empty_script_reply_is_not_a_duplicate(harness, monkeypatch, reply):
    """A reply that says nothing must not crash the request.

    Element 0 was guarded against a falsy reply but element 1 was not, so
    `len(None)` raised -- and with the swallow gone that 503s every request
    under fail-closed. Parametrized over both shapes Redis can return.
    """

    # One request first: the Redis client is resolved lazily from app.state on
    # the first dispatch, so there is nothing to patch until it has run.
    assert harness.post(b'{"warm":"up"}').status_code == 200

    async def _empty(*_args, **_kwargs):
        return reply

    monkeypatch.setattr(harness.middleware._redis, "eval", _empty)

    assert harness.post().status_code == 200
    harness.assert_nothing_was_swallowed()


def test_an_unreadable_body_is_not_treated_as_an_empty_one(harness, monkeypatch):
    """A request we could not read is not a request we can call a duplicate.

    `_get_request_body` used to swallow read errors and return `None`, which the
    hasher maps to `b""` -- so an unreadable body deduplicated against a
    genuinely empty one and against every other unreadable body. Now that a
    duplicate is answered 409, that collision is user-visible.
    """
    calls = {"n": 0}

    async def _fail_second_read(_self, request):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("stream consumed")
        return b""

    monkeypatch.setattr(DeduplicationMiddleware, "_get_request_body", _fail_second_read)

    # An empty body establishes the digest for b"".
    assert (
        harness.client.post(
            "/api/v1/agent/query",
            content=b"",
            headers={"X-Session-ID": "sess-1", "content-type": "application/json"},
        ).status_code
        == 200
    )

    # The unreadable one must not collide with it.
    assert (
        harness.client.post(
            "/api/v1/agent/query",
            content=b"anything",
            headers={"X-Session-ID": "sess-1", "content-type": "application/json"},
        ).status_code
        == 200
    )


def test_idempotency_bearing_paths_are_skipped(harness):
    """Deduplication must not pre-empt an idempotent replay.

    Protection is installed *outside* the idempotency middleware, so dedup sees
    a request first. A client resending with a stable `Idempotency-Key` expects
    the cached replay; a 409 instead would break that contract. Today the two
    paths the copilot sends a key on -- `POST /api/v1/cases` and the multipart
    turn POST -- are both on the skip list, so the hazard is latent. Pinned here
    so removing either from `_should_skip` fails loudly rather than silently
    breaking idempotent retry.
    """
    from starlette.datastructures import Headers
    from starlette.requests import Request as StarletteRequest

    def _req(path: str, content_type: str) -> StarletteRequest:
        return StarletteRequest(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": Headers({"content-type": content_type}).raw,
            }
        )

    assert harness.middleware._should_skip(_req("/api/v1/cases", "application/json"))
    assert harness.middleware._should_skip(
        _req("/api/v1/cases/abc/messages", "multipart/form-data; boundary=x")
    )
