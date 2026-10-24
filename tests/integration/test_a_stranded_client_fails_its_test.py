"""The integration teardown fails a test that strands a Redis client.

A redis-py asyncio pool can only be closed on the event loop it was opened on.
An app served from a loop that has since ended (a bare ``TestClient`` gives
every request a portal loop of its own) leaves its protection middleware
holding a client nothing can close, which surfaced as ``unclosed transport``
in whichever test was running at garbage collection. The teardown fails the
test that served it instead, naming the app and the middleware.
"""

import asyncio
import threading

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.config.protection import get_development_protection_settings
from tests.integration.conftest import _end_the_service_of

pytestmark = pytest.mark.integration

_RESOLVE = "faultmaven.infrastructure.redis_client.resolve_redis_client"


class _RecordingClient:
    """Not from ``fakeredis``, so the middleware owns it; records its closes."""

    def __init__(self):
        self.closes = 0

    async def aclose(self):
        self.closes += 1


def _served_app(monkeypatch, client) -> FastAPI:
    monkeypatch.setattr(
        _RESOLVE, lambda request, injected=None, **kw: injected or client
    )
    app = FastAPI()
    app.add_middleware(
        DeduplicationMiddleware, settings=get_development_protection_settings()
    )
    app.get("/ping")(lambda: {"ok": True})
    return app


async def _serve_once(app) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/ping")).status_code == 200


def _serve_on_a_loop_that_then_ends(app) -> None:
    failure = []

    def _run():
        try:
            asyncio.run(_serve_once(app))
        except BaseException as e:  # surfaced below
            failure.append(e)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]


async def test_a_client_on_an_ended_loop_fails_naming_the_app_and_middleware(
    monkeypatch,
):
    client = _RecordingClient()
    app = _served_app(monkeypatch, client)
    _serve_on_a_loop_that_then_ends(app)

    with pytest.raises(pytest.fail.Exception) as failed:
        await _end_the_service_of([("the scratch app", app)])

    assert "the scratch app: DeduplicationMiddleware" in str(failed.value)
    assert client.closes == 0


async def test_a_client_on_this_tests_loop_is_closed_and_passes(monkeypatch):
    """Positive control: the same app served from this test's own loop."""
    client = _RecordingClient()
    app = _served_app(monkeypatch, client)
    await _serve_once(app)

    await _end_the_service_of([("the scratch app", app)])

    assert client.closes == 1
