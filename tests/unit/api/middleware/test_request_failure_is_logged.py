"""An unhandled exception must produce its structured failure record.

``LoggingMiddleware`` catches an unhandled exception and emits a
``Request failed`` ERROR carrying method, path, duration, error type and the
correlation id — the record an operator uses to tie a 500 back to a request.

It was unreachable. The middleware called ``add_layer_error("api", e)`` and
only then asked ``should_log_error("api")``, whose rule is "has this layer
already logged?" — so the answer was always no. Cascade prevention was
suppressing the first log rather than the second.

Nothing caught it because the two call sites in
``infrastructure/logging/unified.py`` ask before recording, and the
``ErrorContext`` unit tests exercise the class directly, never this ordering.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware


@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    with (
        patch("faultmaven.api.middleware.logging.request_counter") as counter,
        patch("faultmaven.api.middleware.logging.request_duration") as duration,
        patch("faultmaven.api.middleware.logging.sla_tracker"),
    ):
        counter.labels.return_value = MagicMock()
        duration.labels.return_value = MagicMock()
        yield TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
class TestRequestFailureIsLogged:
    def test_an_unhandled_exception_emits_the_structured_failure_record(
        self, client, caplog
    ):
        with caplog.at_level("DEBUG"):
            response = client.get("/boom")

        assert response.status_code == 500

        failures = [
            record
            for record in caplog.records
            if "Request failed" in record.getMessage()
        ]
        assert failures, "the request-failure ERROR record was never emitted"

        record = failures[0]
        assert record.levelname == "ERROR"
        assert "RuntimeError" in record.getMessage() + repr(record.__dict__)

    def test_the_error_is_still_recorded_for_cascade_prevention(self, client, caplog):
        """Asking first must not stop the layer being marked.

        The fix reorders two calls; dropping the second would also make the
        test above pass, while removing the cascade prevention it exists for.
        """
        with caplog.at_level("DEBUG"):
            client.get("/boom")

        summaries = [
            record
            for record in caplog.records
            if "request summary" in record.getMessage().lower()
        ]
        assert summaries, "no request summary was emitted"
        assert any(
            "'errors_encountered': 1" in repr(record.__dict__) for record in summaries
        ), "the api layer error was not recorded on the context"
