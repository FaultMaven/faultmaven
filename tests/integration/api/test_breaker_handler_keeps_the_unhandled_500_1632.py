"""A breaker or deadline that is not the LLM's answers what it answered before
those classes had a handler (#1632, #1635).

Every external client raises both classes. Any other one escaping a route must
still answer as an exception nothing maps: ``LoggingMiddleware`` logs it under
the request's correlation id, and the 500 names that id in its body (#2294).
The control is a ``RuntimeError`` on the same app; on ``main`` the breaker and
the deadline answer exactly as it does. Answered by the handler itself, even
through ``internal_server_error_handler``, they lose ``correlation_id`` from
the body and gain the headers an ordinary response collects on its way out.
Each stack is the real application, rebuilt (``rebuild_app_under``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.integration._app_rebuild import rebuild_app_under
from tests.unit.api.test_llm_failure_shapes_1635 import _breaker, _deadline

pytestmark = pytest.mark.integration

_FAILURES = {
    "unmapped": lambda: RuntimeError("unmapped fault 1632"),
    "chromadb-breaker": lambda: _breaker(service="ChromaDB"),
    "s3-deadline": lambda: _deadline("s3"),
    "llm-breaker": _breaker,
}


def _raiser(factory):
    async def route():
        raise factory()

    return route


@pytest.fixture(scope="module", params=["deployed", "answered-by-the-500-handler"])
def client(request) -> TestClient:
    """``deployed``: ``PerformanceTrackingMiddleware`` answers the exception and
    ``RequestIdMiddleware`` names the request. Otherwise Starlette's
    ``ServerErrorMiddleware`` answers it through the 500 handler."""
    deployed = request.param == "deployed"
    with (
        patch(
            "faultmaven.bootstrap.middleware._is_test_environment",
            return_value=not deployed,
        ),
        patch("faultmaven.bootstrap.middleware.OPIK_MIDDLEWARE_AVAILABLE", False),
    ):
        app = rebuild_app_under(
            {"SKIP_SERVICE_CHECKS": "false" if deployed else "true"}
        )
    for name, factory in _FAILURES.items():
        app.add_api_route(f"/fault-1632/{name}", _raiser(factory), methods=["GET"])
    return TestClient(app, raise_server_exceptions=False)


def _get(client, name):
    return client.get(f"/fault-1632/{name}", headers={"X-Request-ID": "rid-1632"})


def _shape(response) -> tuple:
    body = response.json()
    return (
        response.status_code,
        {key: value for key, value in body.items() if key != "correlation_id"},
        sorted(name for name in response.headers if name.startswith("x-")),
    )


@pytest.mark.parametrize("name", ["chromadb-breaker", "s3-deadline"])
def test_another_services_failure_answers_as_an_unmapped_exception(client, name):
    response = _get(client, name)
    control = _get(client, "unmapped")

    assert control.status_code == 500
    assert _shape(response) == _shape(control)
    # The id LoggingMiddleware logged the failure under, which only an
    # exception that reached it carries.
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert "Circuit breaker" not in response.text


def test_the_llm_breaker_answers_the_llm_status_through_the_app(client):
    response = _get(client, "llm-breaker")

    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"
    assert response.headers["retry-after"] == "30"
