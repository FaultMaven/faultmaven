"""An unhandled exception's 500 names the request's correlation id (#2294).

Every response that passes back out through ``LoggingMiddleware`` carries
``X-Correlation-ID``, the id on every log line of the request (#2278). An
unhandled exception does not pass back out. The middleware logs it under the
request's id and re-raises, and the request's context ends there. The 500 is
built further out, by one of two layers:

- ``PerformanceTrackingMiddleware``, which sits outside ``LoggingMiddleware``
  wherever ``SKIP_SERVICE_CHECKS`` is off, as it is on every deployment. It
  catches the exception and answers it itself.
- Otherwise Starlette's ``ServerErrorMiddleware``, the outermost layer, answers
  through the 500 handler ``main.py`` registers, and re-raises.

Neither named the request's id, so the response a user is most likely to report
was the one carrying nothing the request's log lines carry.

Each stack here is the real application, rebuilt with a route that raises. The
rebuild (``rebuild_app_under``) is an app no other test holds, so the route is
added to nothing shared. Its log is read the way the #2278 tests read it: each
record is rendered at emit through the FaultMaven root handler's pre-chain.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.integration._app_rebuild import rebuild_app_under
from tests.integration.api.test_one_correlation_id_per_request_2278 import (
    _at_debug,
    _line,
    _log_lines,
)

pytestmark = pytest.mark.integration

FAULT = "unhandled fault 2294"
FAULT_PATH = "/fault-2294"


def _app_with_an_unhandled_fault(*, skip_service_checks: bool):
    app = rebuild_app_under(
        {"SKIP_SERVICE_CHECKS": "true" if skip_service_checks else "false"}
    )

    @app.get(FAULT_PATH)
    async def unhandled_fault():
        raise RuntimeError(FAULT)

    return app


def _layers(app) -> list[str]:
    return [entry.cls.__name__ for entry in app.user_middleware]


@pytest.fixture(scope="module")
def answered_by_the_500_handler():
    """No layer outside ``LoggingMiddleware`` catches the exception, so
    ``ServerErrorMiddleware`` answers it through the 500 handler."""
    app = _app_with_an_unhandled_fault(skip_service_checks=True)
    assert "PerformanceTrackingMiddleware" not in _layers(app), _layers(app)
    return app


@pytest.fixture(scope="module")
def answered_by_performance_tracking():
    """The deployed stack: ``PerformanceTrackingMiddleware`` answers it."""
    app = _app_with_an_unhandled_fault(skip_service_checks=False)
    layers = _layers(app)
    assert "PerformanceTrackingMiddleware" in layers, layers
    # Starlette wraps in reverse registration order: a lower index is further
    # out. Outside ``LoggingMiddleware`` is what makes this stack the case.
    assert layers.index("PerformanceTrackingMiddleware") < layers.index(
        "LoggingMiddleware"
    ), layers
    return app


class _Sent:
    """What the server sent, recorded outside every layer of the app.

    Lets one request show both halves: the 500 the client received, and the
    exception still raised to the server, which ``TestClient`` re-raises and
    so leaves no response of its own to read.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""

    async def __call__(self, scope, receive, send) -> None:
        async def record(message) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.headers = {
                    name.decode("latin-1").lower(): value.decode("latin-1")
                    for name, value in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                self.body += message.get("body", b"")
            await send(message)

        await self.app(scope, receive, record)


def _assert_the_500_names_the_requests_id(lines, headers, body) -> None:
    request_id = headers.get("x-correlation-id")
    assert request_id, f"no X-Correlation-ID on the 500: {dict(headers)}"

    # The id a user reports, in the body under the name the other error bodies
    # use, and nothing a caller cannot find on the request's lines.
    assert body == {"detail": "Internal server error", "correlation_id": request_id}

    # Not vacuous: the request's own lines are here and carry it.
    for start in ("Request started", "Request failed"):
        assert _line(lines, start).get("correlation_id") == request_id, start
    ids = {line["correlation_id"] for line in lines if "correlation_id" in line}
    assert ids == {request_id}, f"more than one correlation id: {ids}"

    # Logged once, under that id: the layer that answers does not log the
    # exception a second time, under no id.
    naming_the_fault = [line for line in lines if FAULT in str(line.get("event"))]
    assert [line["event"].split(":")[0] for line in naming_the_fault] == [
        "Request failed"
    ], [line.get("event") for line in naming_the_fault]


def test_the_500_names_the_requests_id_and_the_exception_still_propagates(
    answered_by_the_500_handler,
):
    sent = _Sent(answered_by_the_500_handler)

    with _log_lines() as lines, _at_debug():
        # ``raise_server_exceptions=True``: the exception still reaches the
        # server after the 500 is sent, as tests that expect it depend on.
        with pytest.raises(RuntimeError, match=FAULT):
            TestClient(sent, raise_server_exceptions=True).get(FAULT_PATH)

    assert sent.status == 500
    _assert_the_500_names_the_requests_id(lines, sent.headers, json.loads(sent.body))


def test_the_deployed_stacks_500_names_the_requests_id(
    answered_by_performance_tracking,
):
    client = TestClient(answered_by_performance_tracking, raise_server_exceptions=False)

    with _log_lines() as lines, _at_debug():
        response = client.get(FAULT_PATH)

    assert response.status_code == 500, response.text
    _assert_the_500_names_the_requests_id(lines, response.headers, response.json())
