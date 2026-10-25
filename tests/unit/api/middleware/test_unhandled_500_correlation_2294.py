"""The 500 for an unhandled exception, with and without the request's id (#2294).

``LoggingMiddleware`` logs an unhandled exception under the request's
correlation id and re-raises it; the app's 500 handler answers it outside the
middleware, after the request's context has ended. The middleware hands the
handler the id on ``request.state``, so the 500 names it.

These drive the handler ``main.py`` registers, attached to a scratch app, for
two cases the real stacks in
``tests/integration/api/test_unhandled_500_names_the_requests_id_2294.py`` do
not show:

- ``RequestIdMiddleware`` mounted with nothing between ``LoggingMiddleware`` and
  ``ServerErrorMiddleware``: the body keeps its ``request_id`` beside the
  correlation id, and the 500 carries its ``X-Request-ID``.
- An exception ``LoggingMiddleware`` never saw: the handler logs it itself,
  path only, and names no id it does not have.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.api.middleware.request_id import RequestIdMiddleware
from tests.unit.api.middleware.test_access_log_omits_credentials import (
    register_500_handler,
)

pytestmark = pytest.mark.unit

FAULT = "unhandled fault 2294"
SECRET = "authcode-2294-c2VjcmV0"


@pytest.fixture(autouse=True)
def _metrics_stood_in():
    with (
        patch("faultmaven.api.middleware.logging.request_counter") as counter,
        patch("faultmaven.api.middleware.logging.request_duration") as duration,
        patch("faultmaven.api.middleware.logging.sla_tracker"),
    ):
        counter.labels.return_value = MagicMock()
        duration.labels.return_value = MagicMock()
        yield


def _faulting_app() -> FastAPI:
    app = FastAPI()

    @app.get("/fault")
    async def fault():
        raise RuntimeError(FAULT)

    register_500_handler(app)
    return app


def test_the_body_keeps_the_request_id_beside_the_correlation_id():
    """``RequestIdMiddleware`` runs inside ``LoggingMiddleware``, as
    ``setup_middleware`` stacks them, and takes the caller's ``X-Request-ID``."""
    app = _faulting_app()
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(LoggingMiddleware)

    response = TestClient(app, raise_server_exceptions=False).get(
        "/fault", headers={"X-Request-ID": "chosen-by-the-caller-2294"}
    )

    assert response.status_code == 500
    correlation_id = response.headers.get("X-Correlation-ID")
    assert correlation_id, "no X-Correlation-ID on the 500"
    assert correlation_id != "chosen-by-the-caller-2294"
    assert response.json() == {
        "detail": "Internal server error",
        "correlation_id": correlation_id,
        "request_id": "chosen-by-the-caller-2294",
    }
    # RequestIdMiddleware sets its header on every response it returns; the
    # exception left it none, so the 500 carries it.
    assert response.headers.get("X-Request-ID") == "chosen-by-the-caller-2294"


def test_an_exception_logging_middleware_never_saw_is_logged_once_by_the_handler(
    caplog,
):
    """Raised outside ``LoggingMiddleware``: nothing logged it, and no
    correlation id was ever given to the request."""

    class _FailsBeforeTheRequestIsServed:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:
            if scope["type"] == "http":
                raise RuntimeError(FAULT)
            await self.app(scope, receive, send)

    app = _faulting_app()
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(_FailsBeforeTheRequestIsServed)

    with caplog.at_level("DEBUG"):
        response = TestClient(app, raise_server_exceptions=False).get(
            f"/fault?code={SECRET}"
        )

    assert response.status_code == 500
    assert "X-Correlation-ID" not in response.headers
    assert response.json() == {"detail": "Internal server error"}

    naming_the_fault = [
        record
        for record in caplog.records
        if record.name.startswith("faultmaven") and FAULT in record.getMessage()
    ]
    assert len(naming_the_fault) == 1, [r.getMessage() for r in naming_the_fault]
    (record,) = naming_the_fault
    assert "GET /fault" in record.getMessage()
    assert SECRET not in record.getMessage()
    # The one record of this exception carries its traceback.
    assert record.exc_info and record.exc_info[0] is RuntimeError
