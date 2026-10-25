"""A request has one correlation id: the body's Error ID, the header and every
log line of the request agree (#2278).

``LoggingMiddleware`` gives each request a correlation id. It stamps the id on
every log line of the request and returns it as ``X-Correlation-ID``. Routes
used to mint a second id. They logged it on their own error line, set it as a
header the middleware then overwrote, and named it in the turn route's 500
body as the ``Error ID``. A user who reported that Error ID handed the
operator an id found on one line only, and on neither the header nor the
request's own lines.

These tests drive the real application: the booted app, its middleware stack
and its exception handlers. A route's ``HTTPException`` is rendered inside
``LoggingMiddleware``, so the response carries ``X-Correlation-ID``, the 4xx
and the 500 alike. Each record is rendered as it is emitted, in the request's
own context, through the FaultMaven root handler's pre-chain. That chain is
where ``add_request_context`` stamps the request's id on a line that names
none. So what is read here is what the log carries.

The middleware inside ``LoggingMiddleware`` answers within the request's
context too, so a refusal body it writes names the request's id. Here, not the
``x-correlation-id`` the caller sent (``DeduplicationMiddleware``, mounted on
this app). ``IdempotencyMiddleware``, unmounted under ``SKIP_SERVICE_CHECKS``,
is held to it in ``tests/unit/api/middleware/test_idempotency_caller_scoping.py``.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.infrastructure.logging.config import FaultMavenLogger, get_logger
from faultmaven.main import app as main_app
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)

pytestmark = pytest.mark.integration

CASE_ID = "case_2278_one_id"

USER = UserDTO(
    user_id="user_2278",
    username="user2278",
    email="user2278@example.com",
    display_name="User 2278",
    is_active=True,
)


class _RenderedAtEmit(logging.Handler):
    """Each record, rendered to JSON when it is emitted.

    Rendering at emit matters: a plain stdlib record gets the request's id from
    the formatter's pre-chain, which reads the request context, so a record
    formatted after the request has ended would show no id at all.
    """

    def __init__(self, pre_chain) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=pre_chain,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@contextmanager
def _log_lines():
    """The lines the application logs, as the FaultMaven root handler
    builds them."""
    get_logger(__name__)  # the root handler is installed on first use
    root = logging.getLogger()
    marked = [
        handler
        for handler in root.handlers
        if getattr(handler, FaultMavenLogger._ROOT_HANDLER_MARKER, False)
    ]
    assert len(marked) == 1, f"expected one FaultMaven root handler: {marked}"
    capture = _RenderedAtEmit(marked[0].formatter.foreign_pre_chain)
    root.addHandler(capture)
    try:
        yield capture.lines
    finally:
        root.removeHandler(capture)


@contextmanager
def _at_debug():
    """Every level reaches the root's handlers for the duration."""
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.setLevel(level)


def _line(lines, start: str) -> dict:
    matches = [line for line in lines if str(line.get("event", "")).startswith(start)]
    assert len(matches) == 1, (start, [line.get("event") for line in lines])
    return matches[0]


def _the_one_id(lines, response, *route_lines: str) -> str:
    """The request's id, after checking every place it appears agrees."""
    header = response.headers.get("X-Correlation-ID")
    assert header, f"no X-Correlation-ID on the {response.status_code}"

    # Not vacuous: the request's own lines and the route's are all here.
    for start in ("Request started", "Request completed", *route_lines):
        assert _line(lines, start).get("correlation_id") == header, start

    ids = {line["correlation_id"] for line in lines if "correlation_id" in line}
    assert ids == {header}, f"more than one correlation id: {ids}"
    return header


@pytest.fixture
def case_service():
    service = MagicMock()
    service.get_case = AsyncMock(return_value=MagicMock(is_terminal=False))
    return service


@pytest.fixture
def investigation_service():
    service = MagicMock()
    service.prepare_turn = AsyncMock(side_effect=RuntimeError("engine fault 2278"))
    return service


@pytest.fixture
def client(booted_app_client, case_service, investigation_service):
    """The shared booted app, with the caller, the case store and the engine
    stood in for. ``main_app`` is the process's one application, so the
    overrides come off afterwards."""
    overrides = {
        require_authentication: lambda: USER,
        _di_get_case_service_dependency: lambda: case_service,
        get_investigation_service: lambda: investigation_service,
    }
    main_app.dependency_overrides.update(overrides)
    try:
        yield booted_app_client
    finally:
        for dependency in overrides:
            main_app.dependency_overrides.pop(dependency, None)


def test_a_turns_500_names_the_requests_id_in_body_header_and_lines(client):
    with _log_lines() as lines, _at_debug():
        response = client.post(
            f"/api/v1/cases/{CASE_ID}/turns", data={"query": "why is it down"}
        )

    assert response.status_code == 500, response.text
    # The id a user reports, first: it is the header's, and then the lines'.
    assert response.json()["detail"] == (
        "An unexpected error occurred. "
        f"Error ID: {response.headers.get('X-Correlation-ID')}"
    )
    _the_one_id(lines, response, "Unexpected error processing turn")


def test_a_routes_4xx_carries_the_requests_id_on_header_and_lines(client, case_service):
    """A refused delete: the route's own warning line and the 403 carry the
    request's id."""
    case_service.hard_delete_case = AsyncMock(return_value=False)

    with _log_lines() as lines, _at_debug():
        response = client.delete(f"/api/v1/cases/{CASE_ID}")

    assert response.status_code == 403, response.text
    _the_one_id(lines, response, "Refused delete of case")


def test_a_duplicate_refusal_names_the_requests_id_not_the_callers(
    booted_app_client,
):
    """``DeduplicationMiddleware`` echoed the caller's ``x-correlation-id``
    header in its 409 body, so a client chose the id the refusal named."""
    headers = {
        "X-Session-ID": f"sess-2278-{uuid.uuid4().hex}",
        "x-correlation-id": "chosen-by-the-caller",
    }
    body = {"query": f"dedup-2278-{uuid.uuid4().hex}"}

    booted_app_client.post("/api/v1/cases/search", json=body, headers=headers)
    duplicate = booted_app_client.post(
        "/api/v1/cases/search", json=body, headers=headers
    )

    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.headers["x-error-code"] == "DUPLICATE_REQUEST"
    request_id = duplicate.headers.get("X-Correlation-ID")
    assert request_id and request_id != "chosen-by-the-caller"
    assert duplicate.json()["correlation_id"] == request_id
