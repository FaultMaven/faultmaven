"""HTTP request metrics and client attribution in LoggingMiddleware.

Regression tests for the Phase 3 follow-up: http_requests_total /
http_request_duration_seconds were defined in the metrics shim but never
recorded — the Phase 4 alert rules and dashboards reference them, so the
middleware must populate them with bounded-cardinality labels.

Also pinned here: the ``client_ip`` a log line is stamped with must be the same
address the limiters enforce on. It used to be the raw socket peer, which
behind an ingress is the ingress pod for every request on the deployment — so
every log line named the proxy and forensics attributed a flood to the wrong
party, while the limit that refused it had been applied to somebody else.
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.infrastructure.logging.coordinator import LoggingCoordinator


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

    @app.get("/api/v1/cases/{case_id}/gone")
    async def gone(case_id: str):
        raise HTTPException(status_code=404, detail="no such case")

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

        # A 404 on a MATCHED route: the service surface answered, and it
        # answered correctly. (A 404 on no route at all is `unmatched` and is
        # not observed — see TestSlaObservationIsTheServiceSurface.)
        client.get("/api/v1/cases/abc-123/gone")

        assert sla.record_request_metrics.call_args.kwargs["success"] is True


# Measured on this box against the running stack, 2026-09-19 (40 samples):
# /readiness 5-14 ms, /health 67-83 ms. Three real investigation turns on
# gemini-3.7-flash took 6.18 / 5.70 / 5.98 s; #1522 measured 5.88-14.95 s.
PROBE_SECONDS = 0.083
TURN_SECONDS = 14.95


def _drive(template: str, seconds: float, *, method: str = "get", boom: bool = False):
    """One request to ``template`` that takes ``seconds``; returns the mocks.

    The clock is advanced by the ROUTE HANDLER, not by a fixed list of ticks:
    anything on the path may read ``time.time()`` any number of times, and a
    side_effect list silently measured 0.0s when it did.

    Returns ``(counter, duration, sla, response)``.
    """
    now = [1000.0]

    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    async def handler():
        now[0] = 1000.0 + seconds
        if boom:
            raise RuntimeError("kaboom")
        return {"ok": True}

    getattr(app, method)(template)(handler)
    client = TestClient(app, raise_server_exceptions=False)
    url = re.sub(r"\{[^}]+\}", "sample-id", template)

    with (
        patch("faultmaven.api.middleware.logging.request_counter") as counter,
        patch("faultmaven.api.middleware.logging.request_duration") as duration,
        patch("faultmaven.api.middleware.logging.sla_tracker") as sla,
    ):
        counter.labels.return_value = MagicMock()
        duration.labels.return_value = MagicMock()
        with patch(
            "faultmaven.api.middleware.logging.time.time", side_effect=lambda: now[0]
        ):
            response = client.request(method.upper(), url)
        return counter, duration, sla, response


@pytest.mark.unit
class TestSlaObservationIsTheServiceSurface:
    """#1523: the SLA tracker observes real API traffic, not the pollers.

    Kubelet polls ``/health`` and ``/readiness`` and Prometheus scrapes
    ``/metrics``; on any deployment they outnumber real requests by orders of
    magnitude and never 5xx. Feeding them in made BOTH tracker numbers
    describe the pollers: a p95 that is the probe p95 (66.7 ms measured on
    this box while three real 6-second turns were in the same window), and an
    availability whose denominator is ~97% requests that cannot fail.

    The excluded set is byte-for-byte the one every API SLO recording rule in
    ``faultmaven-enterprise-infra`` already excludes —
    ``endpoint!~"/health.*|/metrics.*|/readiness.*|unmatched"``.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/health",
            "/health/sla",
            "/health/components/{name}",
            "/readiness",
            "/metrics",
            "/metrics/performance",
        ],
    )
    def test_a_probe_endpoint_is_not_observed(self, path):
        _, duration, sla, response = _drive(path, PROBE_SECONDS)

        assert response.status_code == 200
        sla.record_request_metrics.assert_not_called()
        # Positive control: the request really ran and really was timed, so
        # "not observed" cannot be satisfied by a middleware that did nothing.
        assert duration.labels.return_value.observe.call_args[0][0] == pytest.approx(
            PROBE_SECONDS
        )

    def test_an_unmatched_path_is_not_observed(self):
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)
        client = TestClient(app, raise_server_exceptions=False)

        with (
            patch("faultmaven.api.middleware.logging.request_counter") as counter,
            patch("faultmaven.api.middleware.logging.request_duration"),
            patch("faultmaven.api.middleware.logging.sla_tracker") as sla,
        ):
            counter.labels.return_value = MagicMock()
            response = client.get("/no/such/route/9999")

        assert response.status_code == 404
        assert counter.labels.call_args.kwargs["endpoint"] == "unmatched"
        sla.record_request_metrics.assert_not_called()

    def test_the_products_main_path_is_observed_at_its_real_duration(self):
        """A 14.95s turn — the slowest healthy one measured — is real traffic.

        This is the half that must not be over-filtered. It also pins the
        DURATION, so a filter that recorded turns but measured them at 0.0s
        (which would restore probe-shaped numbers by another route) fails.
        """
        _, _, sla, response = _drive(
            "/api/v1/cases/{case_id}/turns", TURN_SECONDS, method="post"
        )

        assert response.status_code == 200
        sla.record_request_metrics.assert_called_once()
        args, kwargs = sla.record_request_metrics.call_args
        assert args[0] == "api"
        assert args[1] == pytest.approx(TURN_SECONDS * 1000.0)
        assert kwargs["success"] is True

    def test_an_exception_on_a_probe_is_not_observed_either(self):
        """Both recording sites, or the filter leaks on the failure path."""
        _, duration, sla, response = _drive("/health", PROBE_SECONDS, boom=True)

        assert response.status_code == 500
        sla.record_request_metrics.assert_not_called()
        assert duration.labels.return_value.observe.call_args[0][0] == pytest.approx(
            PROBE_SECONDS
        )

    def test_an_exception_on_the_service_surface_is_observed_as_a_failure(self):
        _, _, sla, response = _drive(
            "/api/v1/cases/{case_id}/turns", TURN_SECONDS, method="post", boom=True
        )

        assert response.status_code == 500
        sla.record_request_metrics.assert_called_once()
        assert sla.record_request_metrics.call_args.args[1] == pytest.approx(
            TURN_SECONDS * 1000.0
        )
        assert sla.record_request_metrics.call_args.kwargs["success"] is False


INGRESS = "10.42.0.7"
INGRESS_RANGE = "10.42.0.0/16"
CLIENT = "203.0.113.44"


def _logged_context(monkeypatch, trusted, peer, headers):
    """Drive the middleware and return the context it built for the request."""
    captured = {}

    real_start = LoggingCoordinator.start_request

    def _capture(self, **kwargs):
        captured.update(kwargs.get("attributes") or {})
        return real_start(self, **kwargs)

    monkeypatch.setattr(LoggingCoordinator, "start_request", _capture)
    if trusted is None:
        monkeypatch.delenv("PROTECTION_TRUSTED_PROXIES", raising=False)
    else:
        monkeypatch.setenv("PROTECTION_TRUSTED_PROXIES", trusted)

    app = FastAPI()
    # Constructed AFTER the environment is set, exactly as the real stack is
    # built after ``main`` has loaded the environment.
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    with TestClient(app, client=(peer, 1234)) as client:
        client.get("/probe", headers=headers)

    return captured


@pytest.mark.unit
@pytest.mark.security
class TestLoggedClientIpMatchesEnforcementIdentity:
    """One address, or the audit trail and the limit describe different callers."""

    def test_a_trusted_proxys_forwarded_client_is_logged(self, monkeypatch):
        context = _logged_context(
            monkeypatch,
            trusted=INGRESS_RANGE,
            peer=INGRESS,
            headers={"X-Forwarded-For": CLIENT},
        )

        assert context["client_ip"] == CLIENT

    def test_without_trusted_proxies_the_peer_is_logged(self, monkeypatch):
        """The forged-header direction: an unconfigured deployment believes nothing."""
        attacker = "198.51.100.77"

        context = _logged_context(
            monkeypatch,
            trusted=None,
            peer=attacker,
            headers={"X-Forwarded-For": "1.2.3.4"},
        )

        assert context["client_ip"] == attacker


@pytest.mark.unit
class TestNoPerRequestLatencyVerdict:
    """#1346: a slow request must not produce a WARNING of its own.

    ``LoggingMiddleware`` used to compare whole-request duration against a
    single ``api: 0.1`` constant and log ``"Slow request detected: ... took
    4.812s (threshold: 0.100s)"`` at WARNING. Every investigation turn makes
    at least one LLM call — 5.9s to 15.0s measured on healthy turns against a
    real case — so the line fired on all healthy traffic on the product's main
    path and no alert could be built on it.

    The clock is driven to a realistic duration rather than to some token
    value above a small threshold: a guard that only proves "0.2s does not
    warn" would keep passing if somebody re-introduced the constant at 5.0s.
    """

    def _request_taking(self, seconds: float, caplog):
        """Drive one request whose measured duration is ``seconds``.

        The clock is advanced by the ROUTE HANDLER rather than by counting
        ``time.time()`` calls, so the fake means "the handler took `seconds`"
        no matter how many times anything on the path reads the clock. An
        earlier version handed out a fixed pair of ticks and silently measured
        0.0s because something reads the clock before ``start_time`` is taken
        — which the positive-control test below is what caught.
        """
        now = [1000.0]

        app = FastAPI()
        app.add_middleware(LoggingMiddleware)

        @app.post("/api/v1/cases/{case_id}/turns")
        async def turn(case_id: str):
            now[0] = 1000.0 + seconds
            return {"id": case_id}

        client = TestClient(app)

        with caplog.at_level("DEBUG"):
            with patch(
                "faultmaven.api.middleware.logging.time.time",
                side_effect=lambda: now[0],
            ):
                response = client.post("/api/v1/cases/abc-123/turns")

        assert response.status_code == 200
        return caplog.records

    def test_a_multi_second_request_emits_no_warning(self, caplog, metrics_mocks):
        """The reported symptom, at the duration the issue reported it at."""
        records = self._request_taking(4.812, caplog)

        offending = [
            r
            for r in records
            if r.levelno >= 30 and "slow request" in r.getMessage().lower()
        ]
        assert not offending, (
            "a healthy multi-second turn logged a latency warning: "
            f"{[r.getMessage() for r in offending]}"
        )

    def test_the_slowest_measured_healthy_turn_emits_no_warning(
        self, caplog, metrics_mocks
    ):
        """15.0s — the slowest healthy turn measured — is still not a fault."""
        records = self._request_taking(14.95, caplog)

        assert not [r for r in records if r.levelno >= 30], (
            "no record at WARNING or above belongs to a healthy turn: "
            f"{[(r.levelname, r.getMessage()) for r in records if r.levelno >= 30]}"
        )

    def test_a_healthy_turn_is_not_counted_as_a_performance_violation(
        self, caplog, metrics_mocks
    ):
        """The second channel a recorded timing feeds.

        ``LoggingCoordinator.end_request`` re-derives violations from
        ``performance_tracker.layer_timings`` against the same thresholds, so
        removing only the WARNING would have left the summary line still
        reporting a violation on every healthy turn. Nothing records a
        whole-request timing now, so there is nothing for it to count.
        """
        records = self._request_taking(14.95, caplog)

        summary = [r for r in records if "Request summary" in r.getMessage()]
        assert summary, "the summary line is the second consumer of the timing"
        assert (
            "0 performance violations" in summary[0].getMessage()
        ), f"a healthy turn was counted as a violation: {summary[0].getMessage()}"

    def test_the_duration_is_still_observed_and_still_logged(
        self, caplog, metrics_mocks
    ):
        """Positive control: deleting the verdict must not delete the signal.

        Without this, a middleware that failed to run at all would satisfy the
        two assertions above. The duration has to survive on both channels —
        the histogram (what alerting reads) and the completion line (what a
        self-hosted operator with no Prometheus reads).
        """
        _, duration_metric, _ = metrics_mocks
        records = self._request_taking(4.812, caplog)

        observed = duration_metric.labels.return_value.observe.call_args[0][0]
        assert observed == pytest.approx(4.812)
        assert duration_metric.labels.call_args.kwargs["endpoint"] == (
            "/api/v1/cases/{case_id}/turns"
        )

        completion = [r for r in records if "Request completed" in r.getMessage()]
        assert completion, "the completion line is the log-side latency signal"
        assert "4.812s" in completion[0].getMessage()
