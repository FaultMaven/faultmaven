"""A database failure no handler maps is answered 503, naming the request (#2670).

Wrapped in an exception nothing maps, a database failure reaches
``LoggingMiddleware``, which logs it under the request's correlation id, and is
answered by ``internal_server_error_handler``, from ``PerformanceTrackingMiddleware``
on a deployed stack and from Starlette's ``ServerErrorMiddleware`` otherwise.
The answer is the database's 503, and it names the ids the 500 would have: the
correlation id in the body and ``X-Correlation-ID``, and ``X-Request-ID``. Both
middlewares record the 503 the client receives. Each stack is the real
application, rebuilt (``rebuild_app_under``), as the #1632 tests build it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from faultmaven.api.middleware.performance import PerformanceTrackingMiddleware
from faultmaven.exceptions import DatabaseUnavailable
from tests.integration._app_rebuild import rebuild_app_under

pytestmark = pytest.mark.integration

PATH = "/fault-2670"


def _wrapped_failure():
    async def route():
        try:
            raise DatabaseUnavailable("refused")
        except DatabaseUnavailable as cause:
            raise RuntimeError("a repository's wrapper") from cause

    return route


@pytest.fixture(scope="module", params=["deployed", "answered-by-the-500-handler"])
def stack(request):
    deployed = request.param == "deployed"
    with patch(
        "faultmaven.bootstrap.middleware._is_test_environment",
        return_value=not deployed,
    ):
        app = rebuild_app_under(
            {"SKIP_SERVICE_CHECKS": "false" if deployed else "true"}
        )
    app.add_api_route(PATH, _wrapped_failure(), methods=["GET"])
    return deployed, TestClient(app, raise_server_exceptions=False)


def test_the_503_names_the_request(stack):
    deployed, client = stack

    response = client.get(PATH, headers={"X-Request-ID": "rid-2670"})

    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["retry-after"] == "5"
    body = response.json()
    assert body["correlation_id"] == response.headers["x-correlation-id"]
    if deployed:
        assert body["request_id"] == response.headers["x-request-id"] == "rid-2670"


def test_the_middlewares_record_the_503(stack):
    deployed, client = stack
    counter = MagicMock()
    recorded = AsyncMock()

    with (
        patch("faultmaven.api.middleware.logging.request_counter", counter),
        patch.object(
            PerformanceTrackingMiddleware, "_record_performance_metrics", recorded
        ),
    ):
        client.get(PATH)

    assert counter.labels.call_args.kwargs["status_code"] == "503"
    if deployed:
        assert recorded.await_args.kwargs["status_code"] == 503
