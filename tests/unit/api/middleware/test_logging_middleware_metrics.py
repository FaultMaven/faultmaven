"""HTTP request metrics recording in LoggingMiddleware.

Regression tests for the Phase 3 follow-up: http_requests_total /
http_request_duration_seconds were defined in the metrics shim but never
recorded — the Phase 4 alert rules and dashboards reference them, so the
middleware must populate them with bounded-cardinality labels.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware


@pytest.fixture()
def metrics_mocks():
    with (
        patch("faultmaven.api.middleware.logging.request_counter") as counter,
        patch("faultmaven.api.middleware.logging.request_duration") as duration,
        patch("faultmaven.api.middleware.logging.sla_tracker") as sla,
    ):
        counter.labels.return_value = MagicMock()
        duration.labels.return_value = MagicMock()
        yield counter, duration, sla


@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/api/v1/cases/{case_id}")
    async def get_case(case_id: str):
        return {"id": case_id}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
class TestHttpMetricsRecording:
    def test_success_uses_route_template_not_raw_path(self, client, metrics_mocks):
        counter, duration, _ = metrics_mocks

        response = client.get("/api/v1/cases/abc-123")

        assert response.status_code == 200
        counter.labels.assert_called_once_with(
            method="GET",
            endpoint="/api/v1/cases/{case_id}",
            status_code="200",
        )
        counter.labels.return_value.inc.assert_called_once()
        duration.labels.assert_called_once_with(
            method="GET", endpoint="/api/v1/cases/{case_id}"
        )
        observed = duration.labels.return_value.observe.call_args[0][0]
        assert observed >= 0

    def test_unmatched_route_collapses_to_single_bucket(self, client, metrics_mocks):
        counter, _, _ = metrics_mocks

        response = client.get("/no/such/route/with-an-id-9999")

        assert response.status_code == 404
        assert (
            counter.labels.call_args.kwargs["endpoint"] == "unmatched"
        ), "unmatched paths must not become label values"

    def test_unhandled_exception_records_500(self, client, metrics_mocks):
        counter, duration, sla = metrics_mocks

        response = client.get("/boom")

        assert response.status_code == 500
        counter.labels.assert_called_once_with(
            method="GET", endpoint="/boom", status_code="500"
        )
        duration.labels.return_value.observe.assert_called_once()
        # SLA observation records the failure too
        sla.record_request_metrics.assert_called_once()
        assert sla.record_request_metrics.call_args.kwargs["success"] is False

    def test_4xx_counts_as_served_for_sla(self, client, metrics_mocks):
        _, _, sla = metrics_mocks

        client.get("/no/such/route")

        assert sla.record_request_metrics.call_args.kwargs["success"] is True
