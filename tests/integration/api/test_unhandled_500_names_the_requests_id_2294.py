"""The ids a response carries are the ids the request's log lines carry (#2294).

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
was the one carrying nothing the request's log lines carry. And on every
response ``PerformanceTrackingMiddleware`` replaced ``RequestIdMiddleware``'s
``X-Request-ID`` with an id of its own, which no log line carries.

Each stack here is the real application, rebuilt with routes of its own. The
rebuild (``rebuild_app_under``) is an app no other test holds, so the routes are
added to nothing shared. Its log is read the way the #2278 tests read it: each
record is rendered at emit through the FaultMaven root handler's pre-chain.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.integration._app_rebuild import rebuild_app_under
from tests.integration.api.test_one_correlation_id_per_request_2278 import (
    _at_debug,
    _line,
    _log_lines,
)

pytestmark = pytest.mark.integration

logger = logging.getLogger(__name__)

FAULT = "unhandled fault 2294"
FAULT_PATH = "/fault-2294"
SERVING_THE_FAULT = "Serving the fault route 2294"
OK_PATH = "/ok-2294"
SERVING_OK = "Serving the ok route 2294"
CALLERS_REQUEST_ID = "chosen-by-the-caller-2294"
#: A browser origin the app's CORS allows: a development box appends it.
ALLOWED_ORIGIN = "http://localhost:3333"


def _app_with_routes(*, skip_service_checks: bool, request_ids: bool = False):
    """The real app, rebuilt. ``request_ids`` mounts ``RequestIdMiddleware``,
    which every deployment runs and ``setup_middleware`` skips in a test
    environment, by answering that question "no" for the build. The Opik
    middleware it would also mount stays off: it traces to a server."""
    environment = {"SKIP_SERVICE_CHECKS": "true" if skip_service_checks else "false"}
    if request_ids:
        with (
            patch(
                "faultmaven.bootstrap.middleware._is_test_environment",
                return_value=False,
            ),
            patch("faultmaven.bootstrap.middleware.OPIK_MIDDLEWARE_AVAILABLE", False),
        ):
            app = rebuild_app_under(environment)
    else:
        app = rebuild_app_under(environment)

    @app.get(FAULT_PATH)
    async def unhandled_fault():
        logger.info(SERVING_THE_FAULT)
        raise RuntimeError(FAULT)

    @app.get(OK_PATH)
    async def ok():
        logger.info(SERVING_OK)
        return {"ok": True}

    return app


def _layers(app) -> list[str]:
    return [entry.cls.__name__ for entry in app.user_middleware]


@pytest.fixture(scope="module")
def answered_by_the_500_handler():
    """No layer outside ``LoggingMiddleware`` catches the exception, so
    ``ServerErrorMiddleware`` answers it through the 500 handler."""
    app = _app_with_routes(skip_service_checks=True)
    assert "PerformanceTrackingMiddleware" not in _layers(app), _layers(app)
    return app


@pytest.fixture(scope="module")
def deployed():
    """The deployed stack: ``PerformanceTrackingMiddleware`` outside
    ``LoggingMiddleware`` answers the exception, and ``RequestIdMiddleware``
    inside it names the request."""
    app = _app_with_routes(skip_service_checks=False, request_ids=True)
    layers = _layers(app)
    # Starlette wraps in reverse registration order: a lower index is further
    # out. These positions are what make this stack the deployed case.
    assert (
        layers.index("PerformanceTrackingMiddleware")
        < layers.index("LoggingMiddleware")
        < layers.index("RequestIdMiddleware")
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
    correlation_id = headers.get("x-correlation-id")
    assert correlation_id, f"no X-Correlation-ID on the 500: {dict(headers)}"

    # The id a user reports, in the body under the name the other error bodies
    # use, and nothing a caller cannot find on the request's lines.
    assert body.get("correlation_id") == correlation_id, body
    assert body.get("detail") == "Internal server error", body

    # Not vacuous: the request's own lines are here and carry it.
    for start in ("Request started", SERVING_THE_FAULT, "Request failed"):
        assert _line(lines, start).get("correlation_id") == correlation_id, start
    ids = {line["correlation_id"] for line in lines if "correlation_id" in line}
    assert ids == {correlation_id}, f"more than one correlation id: {ids}"

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
    body = json.loads(sent.body)
    _assert_the_500_names_the_requests_id(lines, sent.headers, body)
    # No RequestIdMiddleware in a pytest-built app, so no id of its own.
    assert set(body) == {"detail", "correlation_id"}, body


def test_the_deployed_stacks_500_names_the_requests_ids(deployed):
    client = TestClient(deployed, raise_server_exceptions=False)

    with _log_lines() as lines, _at_debug():
        response = client.get(FAULT_PATH)

    assert response.status_code == 500, response.text
    body = response.json()
    _assert_the_500_names_the_requests_id(lines, response.headers, body)

    # RequestIdMiddleware's id too, in the body and its header, and it is the
    # one on the route's line.
    request_id = _line(lines, SERVING_THE_FAULT).get("request_id")
    assert request_id, "the route's line carries no request_id"
    assert body == {
        "detail": "Internal server error",
        "correlation_id": response.headers["X-Correlation-ID"],
        "request_id": request_id,
    }
    assert response.headers.get("X-Request-ID") == request_id


@pytest.mark.parametrize("sent", [CALLERS_REQUEST_ID, None], ids=["sent", "minted"])
def test_the_deployed_stacks_x_request_id_is_the_one_on_the_requests_lines(
    deployed, sent
):
    """``RequestIdMiddleware`` takes the caller's ``X-Request-ID`` or mints
    one, binds it to every line of the request and returns it. Nothing outside
    it replaces the header."""
    client = TestClient(deployed)
    headers = {"X-Request-ID": sent} if sent else {}

    with _log_lines() as lines, _at_debug():
        response = client.get(OK_PATH, headers=headers)

    assert response.status_code == 200, response.text
    request_id = _line(lines, SERVING_OK).get("request_id")
    assert request_id, "the route's line carries no request_id"
    assert response.headers.get("X-Request-ID") == request_id
    if sent:
        assert request_id == sent


def test_a_cross_origin_caller_can_read_both_ids(deployed):
    """A browser hands a cross-origin script only the headers the response
    exposes. The Copilot is such a caller."""
    response = TestClient(deployed).get(OK_PATH, headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    exposed = {
        name.strip().lower()
        for name in response.headers.get("access-control-expose-headers", "").split(",")
    }
    assert {"x-correlation-id", "x-request-id"} <= exposed, exposed
