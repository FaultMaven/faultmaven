"""A breaker, a deadline or an ``LLMProviderError`` escaping a route answers as
on ``main`` (#1632, #1635).

No global handler takes these classes: every external client raises the first
two, and the one route that reaches the LLM with them, ``convert_document``,
answers them itself. So one escaping any other route still answers as an
exception nothing maps: ``LoggingMiddleware`` logs it under the request's
correlation id, and the 500 names that id in its body (#2294). The control is a
``RuntimeError`` on the same app. A handler answering them, even through
``internal_server_error_handler``, drops ``correlation_id`` from the body and
adds the headers an ordinary response collects on its way out. Each stack is
the real application, rebuilt (``rebuild_app_under``).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from faultmaven.models.exceptions import LLMProviderError
from tests.integration._app_rebuild import rebuild_app_under
from tests.unit.api.test_llm_failure_shapes_1635 import _breaker, _deadline

pytestmark = pytest.mark.integration

_FAILURES = {
    "unmapped": lambda: RuntimeError("unmapped fault 1632"),
    "chromadb-breaker": lambda: _breaker(service="ChromaDB"),
    "unnamed-breaker": lambda: _breaker(service=None),
    "s3-deadline": lambda: _deadline("s3"),
    "provider-error": lambda: LLMProviderError("no provider"),
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
    with patch(
        "faultmaven.bootstrap.middleware._is_test_environment",
        return_value=not deployed,
    ):
        app = rebuild_app_under(
            {"SKIP_SERVICE_CHECKS": "false" if deployed else "true"}
        )
    for name, factory in _FAILURES.items():
        app.add_api_route(f"/fault-1632/{name}", _raiser(factory), methods=["GET"])
    return TestClient(app, raise_server_exceptions=False)


def _get(client, name):
    return client.get(f"/fault-1632/{name}", headers={"X-Request-ID": "rid-1632"})


# Set only when a response took longer than its threshold
# (``faultmaven/api/middleware/performance.py``), so it follows the runner's
# speed, not how the failure was answered (#2654).
_TIMING_HEADERS = frozenset({"x-performance-warning"})


def _shape(response) -> tuple:
    body = response.json()
    return (
        response.status_code,
        {key: value for key, value in body.items() if key != "correlation_id"},
        sorted(
            name
            for name in response.headers
            if name.startswith("x-") and name not in _TIMING_HEADERS
        ),
    )


@pytest.mark.parametrize("name", sorted(set(_FAILURES) - {"unmapped"}))
def test_each_answers_as_an_unmapped_exception(client, name):
    response = _get(client, name)
    control = _get(client, "unmapped")

    assert control.status_code == 500
    assert _shape(response) == _shape(control)
    # The id LoggingMiddleware logged the failure under, which only an
    # exception that reached it carries.
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]
    assert "Circuit breaker" not in response.text


_BODY = {"detail": "Internal server error"}


def test_shape_ignores_only_the_slow_response_header():
    plain = {"X-Correlation-ID": "c", "X-Response-Time": "1ms"}
    answer = httpx.Response(500, headers=plain, json=_BODY)
    slow = httpx.Response(
        500, headers={**plain, "X-Performance-Warning": "slow_response"}, json=_BODY
    )
    assert _shape(slow) == _shape(answer)

    handled = httpx.Response(
        500, headers={**plain, "X-Processing-Time": "0.001s"}, json=_BODY
    )
    uncorrelated = httpx.Response(500, headers={"X-Response-Time": "1ms"}, json=_BODY)
    unavailable = httpx.Response(503, headers=plain, json=_BODY)
    assert _shape(handled) != _shape(answer)
    assert _shape(uncorrelated) != _shape(answer)
    assert _shape(unavailable) != _shape(answer)
