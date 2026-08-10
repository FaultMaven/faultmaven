"""A duplicate request must actually be answered as one.

The middleware built a labelled 409 with a ``Retry-After`` and never reached it.
``_check_redis_duplicate`` returned the stored value from Redis -- the *first
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

These tests pin the outcome a client sees, not the internals, so the same defect
cannot come back through a different route.
"""

import fakeredis.aioredis as fakeredis_aio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.config.protection import get_development_protection_settings

pytestmark = [pytest.mark.unit, pytest.mark.security]


@pytest.fixture(autouse=True)
def _isolated_fakeredis():
    from faultmaven.infrastructure.redis_client import reset_fakeredis_client

    reset_fakeredis_client()
    yield
    reset_fakeredis_client()


def _client(*, fail_open: bool) -> TestClient:
    settings = get_development_protection_settings()
    settings.deduplication_enabled = True
    settings.fail_open_on_redis_error = fail_open

    app = FastAPI()
    app.add_middleware(
        DeduplicationMiddleware,
        settings=settings,
        redis_client=fakeredis_aio.FakeRedis(decode_responses=True),
    )

    @app.post("/api/v1/agent/query")
    async def _query():
        return {"ok": True}

    return TestClient(app)


def _post(client, body: bytes, session: str = "sess-1"):
    return client.post(
        "/api/v1/agent/query",
        content=body,
        headers={"X-Session-ID": session, "content-type": "application/json"},
    )


@pytest.mark.parametrize("fail_open", [True, False])
def test_duplicate_gets_a_labelled_409(fail_open):
    """Parametrized over both failure policies.

    The old behaviour diverged by policy -- 200 when failing open, 503 when
    failing closed -- so pinning only one would have left the other free to
    regress. A duplicate is a duplicate under either setting.
    """
    client = _client(fail_open=fail_open)
    body = b'{"query":"why is the db slow"}'

    first = _post(client, body)
    assert first.status_code == 200

    second = _post(client, body)
    assert second.status_code == 409
    # Unlabelled 409s are read by the Slack agent as "this case is terminal"
    # (see test_conflict_labelling.py) -- this one must carry its code.
    assert second.headers.get("x-error-code")
    assert int(second.headers["Retry-After"]) > 0
    assert second.json()["error_code"] == second.headers["x-error-code"]


def test_a_different_body_is_not_a_duplicate():
    """The half the old normalizer got wrong, pinned end to end."""
    client = _client(fail_open=True)

    assert _post(client, b'{"query":"check order 4232342342"}').status_code == 200
    assert _post(client, b'{"query":"check order 9994442211"}').status_code == 200


def test_a_different_session_is_not_a_duplicate():
    client = _client(fail_open=True)
    body = b'{"query":"why is the db slow"}'

    assert _post(client, body, session="sess-1").status_code == 200
    assert _post(client, body, session="sess-2").status_code == 200


def test_no_session_id_means_no_deduplication():
    """Dedup keys on the session; without one there is nothing to key on."""
    client = _client(fail_open=True)
    body = b'{"query":"why is the db slow"}'

    for _ in range(2):
        response = client.post(
            "/api/v1/agent/query",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200
