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
