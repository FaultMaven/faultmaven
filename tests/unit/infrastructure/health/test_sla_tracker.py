"""Unit tests for the observation-based SLA tracker.

The tracker computes SLA metrics exclusively from real recorded request
observations (no simulation). These tests verify:

- UNKNOWN status with zeroed values when no observations exist
- Correct availability / nearest-rank percentiles / error rate over a window
- Breach detection (availability + error rate), counting, and recovery
- Time-window exclusion of old observations
- Throughput is informational only (never a breach condition)
- Latency is measured and published but never judged (#1523)
- The published object cannot contradict itself (#1523)
- Breach severity for total outage (actual_value == 0) does not divide by zero
- Prometheus gauge publishing runs cleanly with NoOp shims
- Summary / detail report shapes
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.infrastructure.health.sla_tracker import (
    SLAMetrics,
    SLAStatus,
    SLAThresholds,
    SLATracker,
)

# Durations measured end to end on gemini-3.7-flash against a real case
# (#1522), in milliseconds. The slowest healthy turn is the one that matters:
# a guard written at 200ms would survive the budget coming back at 5s.
HEALTHY_TURN_MS = [5880.0, 9650.0, 10310.0, 14950.0]
SLOWEST_HEALTHY_TURN_MS = 14950.0
# /health and /readiness, 40 samples on the dev box (5-83ms); the cluster
# reports 16-34ms. Three orders of magnitude below a turn.
PROBE_MS = 83.0


@pytest.fixture
def tracker():
    """Fresh tracker per test — never the module-level singleton."""
    return SLATracker()


def _record_many(tracker, component, count, success=True, duration_ms=10.0):
    """Record `count` identical observations for a component."""
    for _ in range(count):
        tracker.record_request_metrics(component, duration_ms, success=success)


@pytest.mark.unit
class TestNoObservations:
    """Components with no recorded data must report UNKNOWN, not fabricate."""

    def test_unknown_status_and_zeroed_values(self, tracker):
        metrics = tracker.calculate_sla_metrics("api")

        assert metrics.status == SLAStatus.UNKNOWN
        assert metrics.component_name == "api"
        assert metrics.availability_percentage == 0.0
        assert metrics.response_time_p50 == 0.0
        assert metrics.response_time_p95 == 0.0
        assert metrics.response_time_p99 == 0.0
        assert metrics.error_rate_percentage == 0.0
        assert metrics.throughput_per_minute == 0.0

    def test_no_breaches_created(self, tracker):
        tracker.calculate_sla_metrics("api")

        # Early return path: no breach detection ran at all
        assert not tracker.active_breaches.get("api")
        assert tracker.breach_history == []

    def test_unknown_for_unregistered_component(self, tracker):
        metrics = tracker.calculate_sla_metrics("nonexistent_component")
        assert metrics.status == SLAStatus.UNKNOWN


@pytest.mark.unit
class TestHealthyWindow:
    """All-success observations within the window meet the SLA."""

    def test_meeting_with_full_availability_and_percentiles(self, tracker):
        # Arrange: deterministic durations 1..100 ms, all successful
        for i in range(1, 101):
            tracker.record_request_metrics("api", float(i), success=True)

        # Act
        metrics = tracker.calculate_sla_metrics("api")

        # Assert
        assert metrics.status == SLAStatus.MEETING
        assert metrics.availability_percentage == 100.0
        assert metrics.error_rate_percentage == 0.0
        # Nearest-rank over 1..100: rank = round(f*n + 0.5) - 1
        assert metrics.response_time_p50 == 50.0
        assert metrics.response_time_p95 == 96.0
        assert metrics.response_time_p99 == 100.0
        assert not tracker.active_breaches.get("api")

    def test_percentile_single_observation(self, tracker):
        tracker.record_request_metrics("api", 42.0, success=True)

        metrics = tracker.calculate_sla_metrics("api")

        assert metrics.response_time_p50 == 42.0
        assert metrics.response_time_p95 == 42.0
        assert metrics.response_time_p99 == 42.0


@pytest.mark.unit
class TestBreachDetection:
    """Failures pushing error rate / availability over thresholds breach."""

    def test_error_rate_breach_recorded(self, tracker):
        # Arrange: api thresholds: min_availability=99.9, max_error_rate=0.5
        _record_many(tracker, "api", 90, success=True)
        _record_many(tracker, "api", 10, success=False)

        # Act
        metrics = tracker.calculate_sla_metrics("api")

        # Assert
        assert metrics.status == SLAStatus.BREACHED
        assert metrics.availability_percentage == 90.0
        assert metrics.error_rate_percentage == pytest.approx(10.0)

        active = tracker.active_breaches.get("api", [])
        breached_metrics = {b.metric_type for b in active}
        assert "availability" in breached_metrics
        assert "error_rate" in breached_metrics
        assert all(b.breach_end is None for b in active)

    def test_breaches_24h_counts_active_breaches(self, tracker):
        _record_many(tracker, "api", 90, success=True)
        _record_many(tracker, "api", 10, success=False)

        metrics = tracker.calculate_sla_metrics("api")

        # availability + error_rate breaches both started within 24h
        assert metrics.breaches_24h == 2


@pytest.mark.unit
class TestBreachRecovery:
    """A healthy window after a breach ends the breach on next calculate."""

    def _make_component(self, tracker):
        tracker.set_sla_thresholds(
            "svc",
            SLAThresholds(
                component_name="svc",
                min_availability=90.0,
                max_error_rate=10.0,
                min_throughput=1.0,
            ),
        )

    def test_breach_ends_when_window_recovers(self, tracker):
        self._make_component(tracker)

        # Arrange: breach — 50% failures (availability 50 < 90, error 50 > 10)
        _record_many(tracker, "svc", 5, success=True)
        _record_many(tracker, "svc", 5, success=False)
        breached = tracker.calculate_sla_metrics("svc")
        assert breached.status == SLAStatus.BREACHED
        assert len(tracker.active_breaches["svc"]) == 2

        # Act: enough successes that the window is healthy again
        # (95 success / 100 total -> availability 95%, error rate 5%)
        _record_many(tracker, "svc", 90, success=True)
        recovered = tracker.calculate_sla_metrics("svc")

        # Assert: breaches ended and moved to history
        assert recovered.status == SLAStatus.MEETING
        assert tracker.active_breaches["svc"] == []
        ended = [b for b in tracker.breach_history if b.component_name == "svc"]
        assert len(ended) == 2
        for breach in ended:
            assert breach.breach_end is not None
            assert breach.duration_minutes is not None
            assert breach.duration_minutes >= 0.0

        # Recovered breaches still count toward the 24h breach count
        assert recovered.breaches_24h == 2


@pytest.mark.unit
class TestTimeWindow:
    """Observations outside the requested window are excluded."""

    def test_old_observations_excluded(self, tracker):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        # Old failures (outside 24h window) must not affect the metrics
        for _ in range(10):
            tracker.record_request_metrics("api", 10.0, success=False, timestamp=old)
        _record_many(tracker, "api", 10, success=True)

        metrics = tracker.calculate_sla_metrics("api", time_window_hours=24)

        assert metrics.availability_percentage == 100.0
        assert metrics.error_rate_percentage == 0.0
        assert metrics.status == SLAStatus.MEETING

    def test_only_old_observations_yields_unknown(self, tracker):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        tracker.record_request_metrics("api", 10.0, success=True, timestamp=old)

        metrics = tracker.calculate_sla_metrics("api", time_window_hours=24)

        assert metrics.status == SLAStatus.UNKNOWN
        assert metrics.availability_percentage == 0.0

    def test_narrower_window_excludes_recent_but_outside(self, tracker):
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        for _ in range(5):
            tracker.record_request_metrics(
                "api", 10.0, success=False, timestamp=two_hours_ago
            )
        _record_many(tracker, "api", 5, success=True)

        metrics = tracker.calculate_sla_metrics("api", time_window_hours=1)

        assert metrics.availability_percentage == 100.0


@pytest.mark.unit
class TestThroughputInformational:
    """Low throughput alone must never produce BREACHED status."""

    def test_low_throughput_does_not_breach(self, tracker):
        # api min_throughput is 100/min; record only 3 healthy requests
        _record_many(tracker, "api", 3, success=True, duration_ms=10.0)

        metrics = tracker.calculate_sla_metrics("api")

        assert metrics.throughput_per_minute < 100.0
        assert metrics.status == SLAStatus.MEETING
        assert not tracker.active_breaches.get("api")
        # No throughput breach exists in any breach list
        all_breaches = tracker.breach_history + tracker.active_breaches.get("api", [])
        assert all(b.metric_type != "throughput" for b in all_breaches)


@pytest.mark.unit
class TestLatencyIsMeasuredNotJudged:
    """#1523: no latency budget, because no single number can be right here.

    ``api`` aggregates health probes (83ms) and investigation turns (up to
    14.95s) — a ~500x spread — and ``llm_provider`` aggregates nine providers.
    The shipped 200ms budget reported ``breached`` in production at
    p95=1268ms on a 100%-available API, then ``meeting`` at p95=83ms 82
    minutes after a restart: the verdict tracked the traffic MIX, not the
    service. The per-route verdict lives in Prometheus
    (``faultmaven:slo_api_latency_p95:5m`` → ``FaultMavenAPIHighLatency``).
    """

    def test_the_slowest_healthy_turn_does_not_breach(self, tracker):
        """The regression itself, at the duration it was measured at.

        Written at 14.95s rather than at some token value above 200ms so it
        fails for ANY latency budget a future change could reinstate below
        the slowest healthy turn — not just for the 200ms one that was there.
        """
        for duration_ms in HEALTHY_TURN_MS:
            tracker.record_request_metrics("api", duration_ms, success=True)

        metrics = tracker.calculate_sla_metrics("api")

        assert metrics.response_time_p95 == SLOWEST_HEALTHY_TURN_MS
        assert metrics.status == SLAStatus.MEETING, (
            "a fully available API serving healthy investigation turns must "
            f"not be BREACHED; active breaches: "
            f"{[b.metric_type for b in tracker.active_breaches.get('api', [])]}"
        )
        assert not tracker.active_breaches.get("api")

    def test_probe_latency_does_not_breach_either(self, tracker):
        """The other population. Both pass, which is the point.

        If one of these two ever fails while the other passes, a single
        number has been reintroduced and it is discriminating between
        populations rather than between healthy and broken.
        """
        _record_many(tracker, "api", 40, success=True, duration_ms=PROBE_MS)

        assert tracker.calculate_sla_metrics("api").status == SLAStatus.MEETING

    def test_no_latency_condition_exists_at_all(self, tracker):
        """Read from the one function both consumers use, not from a name.

        ``_breach_checks`` is what ``_determine_sla_status`` and
        ``_check_sla_breaches`` each read, so this is the complete set of
        things that can make a component BREACHED.
        """
        _record_many(tracker, "api", 10, success=True, duration_ms=PROBE_MS)
        metrics = tracker.calculate_sla_metrics("api")
        thresholds = tracker.component_thresholds["api"]

        conditions = {mt for mt, *_ in tracker._breach_checks(metrics, thresholds)}

        assert conditions == {"availability", "error_rate"}

    def test_the_measurement_is_still_published(self, tracker):
        """Positive control: removing the verdict must not remove the number.

        Without this, deleting the percentile computation outright would
        satisfy every assertion above.
        """
        for duration_ms in HEALTHY_TURN_MS:
            tracker.record_request_metrics("api", duration_ms, success=True)

        summary = tracker.get_sla_summary()
        details = tracker.get_component_sla_details("api")

        assert summary["components"]["api"]["response_time_p95"] == (
            SLOWEST_HEALTHY_TURN_MS
        )
        assert details["current_metrics"]["response_time_p95"] == (
            SLOWEST_HEALTHY_TURN_MS
        )
        assert details["current_metrics"]["response_time_p50"] == 9650.0


@pytest.mark.unit
class TestPublishedObjectIsSelfConsistent:
    """#1523: ``status`` never contradicts the numbers printed beside it.

    Production served this, and it is what the issue is named for::

        "api": {"sla": 100.0, "status": "breached",
                "response_time_p95": 1268.3, "error_rate": 0.0}

    100% available, zero errors, and breached. A reader cannot act on that.
    """

    def test_full_availability_is_never_breached(self, tracker):
        """The exact combination the issue reports, at turn latencies."""
        for duration_ms in HEALTHY_TURN_MS:
            tracker.record_request_metrics("api", duration_ms, success=True)

        entry = tracker.get_sla_summary()["components"]["api"]

        assert entry["sla"] == 100.0
        assert entry["error_rate"] == 0.0
        assert entry["status"] == "meeting", (
            "sla=100.0 + error_rate=0.0 + status=breached is the defect: "
            f"got {entry}"
        )

    @pytest.mark.parametrize(
        "successes,failures,duration_ms",
        [
            (100, 0, SLOWEST_HEALTHY_TURN_MS),  # healthy, slow
            (40, 0, PROBE_MS),  # healthy, fast
            (99, 1, SLOWEST_HEALTHY_TURN_MS),  # 1% errors, slow
            (0, 10, SLOWEST_HEALTHY_TURN_MS),  # total outage, slow
            (0, 10, PROBE_MS),  # total outage, fast
        ],
    )
    def test_breached_is_always_explained_by_the_same_object(
        self, tracker, successes, failures, duration_ms
    ):
        """BREACHED implies one of the two published numbers is past its floor.

        Latency is published too, but it cannot be the reason — so a reader
        holding only the summary entry can always name the cause.
        """
        _record_many(tracker, "api", successes, success=True, duration_ms=duration_ms)
        _record_many(tracker, "api", failures, success=False, duration_ms=duration_ms)

        entry = tracker.get_sla_summary()["components"]["api"]
        thresholds = tracker.component_thresholds["api"]

        if entry["status"] == "breached":
            assert (
                entry["sla"] < thresholds.min_availability
                or entry["error_rate"] > thresholds.max_error_rate
            ), f"nothing in {entry} explains the verdict"
        else:
            assert (
                entry["sla"] >= thresholds.min_availability
                and entry["error_rate"] <= thresholds.max_error_rate
            ), f"{entry} is past a threshold but not reported as breached"

    def test_status_and_active_breaches_cannot_disagree(self, tracker):
        """Both readers of ``_breach_checks``, on the same window.

        They used to hold separate copies of the comparisons; one object
        reporting ``meeting`` beside two active breaches is the failure that
        invites.
        """
        _record_many(tracker, "api", 90, success=True)
        _record_many(tracker, "api", 10, success=False)
        breached = tracker.calculate_sla_metrics("api")
        assert breached.status is SLAStatus.BREACHED
        assert tracker.active_breaches["api"]

        # api's floor is 99.9% available, so 10 failures need >=10k requests
        # beside them before the window is healthy again.
        _record_many(tracker, "api", 19_990, success=True)
        recovered = tracker.calculate_sla_metrics("api")

        assert recovered.status is SLAStatus.MEETING
        assert tracker.active_breaches["api"] == []

    def test_compliance_flags_are_exactly_the_breach_conditions(self, tracker):
        """``compliance`` is the per-condition view of the same verdict."""
        _record_many(tracker, "api", 90, success=True)
        _record_many(tracker, "api", 10, success=False)

        details = tracker.get_component_sla_details("api")
        metrics = tracker.component_metrics["api"]
        thresholds = tracker.component_thresholds["api"]

        expected_keys = {
            f"{mt}_compliance" for mt, *_ in tracker._breach_checks(metrics, thresholds)
        }
        assert set(details["compliance"]) == expected_keys

        breached_now = {b.metric_type for b in tracker.active_breaches["api"]}
        for metric_type, *_ in tracker._breach_checks(metrics, thresholds):
            compliant = details["compliance"][f"{metric_type}_compliance"]
            assert compliant is (metric_type not in breached_now)

    def test_an_unobserved_component_claims_no_compliance(self, tracker):
        """``status: unknown`` must not ship ``availability_compliance: false``.

        The zeroed metrics mean "not measured" (#1515). Comparing them
        published a failing verdict for database / knowledge_base /
        session_store on every deployment, none of which anything records for.
        """
        details = tracker.get_component_sla_details("database")

        assert details["current_metrics"]["status"] == "unknown"
        assert details["compliance"] == {
            "availability_compliance": None,
            "error_rate_compliance": None,
        }

    def test_at_risk_is_not_produced(self, tracker):
        """Recorded, not asserted as desirable: AT_RISK is arithmetically dead.

        ``availability_margin < alert_threshold`` requires availability below
        ``0.95 * min_availability``, which is strictly inside "below
        min_availability" and so returned BREACHED first. That was equally
        true before the latency terms were removed. Making it reachable means
        choosing a warning margin, and there is no measurement here to choose
        one from — inventing a second round figure is the defect #1523 is
        about. This test exists so the next reader learns it from the suite
        rather than from a silent gauge.
        """
        thresholds = tracker.component_thresholds["api"]
        assert thresholds.alert_threshold <= 100.0

        for availability in (100.0, 99.95, 99.9, 99.89, 95.0, 94.0, 50.0, 0.0):
            metrics = SLAMetrics(
                component_name="api",
                availability_percentage=availability,
                response_time_p50=PROBE_MS,
                response_time_p95=SLOWEST_HEALTHY_TURN_MS,
                response_time_p99=SLOWEST_HEALTHY_TURN_MS,
                error_rate_percentage=100.0 - availability,
                throughput_per_minute=1.0,
                status=SLAStatus.MEETING,
            )
            assert (
                tracker._determine_sla_status("api", metrics) is not SLAStatus.AT_RISK
            )

    def test_a_deployment_can_still_declare_a_stricter_floor(self, tracker):
        """``replace`` on the dataclass: the two live knobs still bite."""
        strict = replace(tracker.component_thresholds["api"], min_availability=100.0)
        tracker.set_sla_thresholds("api", strict)
        _record_many(tracker, "api", 999, success=True)
        _record_many(tracker, "api", 1, success=False)

        assert tracker.calculate_sla_metrics("api").status is SLAStatus.BREACHED


@pytest.mark.unit
class TestBreachSeverity:
    """Severity calculation handles edge values safely."""

    def test_zero_actual_value_is_critical_without_zero_division(self, tracker):
        # Total outage: availability actual_value == 0 must not raise
        severity = tracker._determine_breach_severity("availability", 0.0, 99.9)
        assert severity == "critical"

    def test_higher_is_worse_metric_severity(self, tracker):
        assert tracker._determine_breach_severity("error_rate", 2.0, 1.0) == "critical"
        assert tracker._determine_breach_severity("error_rate", 1.6, 1.0) == "high"
        assert tracker._determine_breach_severity("error_rate", 1.25, 1.0) == "medium"
        assert tracker._determine_breach_severity("error_rate", 1.1, 1.0) == "low"


@pytest.mark.unit
class TestPrometheusGauges:
    """update_prometheus_gauges publishes for every default component."""

    def test_runs_without_error_for_all_default_components(self, tracker):
        # Metrics disabled in tests -> NoOp shims; must not raise
        tracker.update_prometheus_gauges()

    def test_runs_without_error_with_real_data_and_breaches(self, tracker):
        _record_many(tracker, "api", 90, success=True)
        _record_many(tracker, "api", 10, success=False)
        _record_many(tracker, "llm_provider", 50, success=True)

        tracker.update_prometheus_gauges()


@pytest.mark.unit
class TestReportShapes:
    """get_sla_summary / get_component_sla_details return expected shapes."""

    def test_sla_summary_shape_with_real_data(self, tracker):
        _record_many(tracker, "api", 50, success=True, duration_ms=20.0)

        summary = tracker.get_sla_summary()

        assert set(summary.keys()) == {
            "overall_sla",
            "components",
            "active_breaches",
            "total_breaches_24h",
            "worst_performing_component",
            "best_performing_component",
        }
        # All default components are present
        assert set(summary["components"].keys()) == set(
            tracker.component_thresholds.keys()
        )
        api_entry = summary["components"]["api"]
        assert set(api_entry.keys()) == {
            "sla",
            "status",
            "response_time_p95",
            "error_rate",
            "breaches_24h",
        }
        assert api_entry["sla"] == 100.0
        assert api_entry["status"] == "meeting"
        # api is the only component with data, so it is both the best and the
        # worst performer. It used to be only the best, because a component
        # with NO observations reported 0.0 availability and so ranked below
        # it — the same "unknown read as zero" that made overall_sla 20.0 on
        # a healthy deployment (#1515). Unobserved components are now ranked
        # nowhere and averaged nowhere.
        assert summary["best_performing_component"] == "api"
        assert summary["worst_performing_component"] == "api"
        assert summary["active_breaches"] == 0
        assert summary["overall_sla"] == 100.0

    def test_sla_summary_counts_active_breaches(self, tracker):
        _record_many(tracker, "api", 90, success=True)
        _record_many(tracker, "api", 10, success=False)

        summary = tracker.get_sla_summary()

        assert summary["components"]["api"]["status"] == "breached"
        assert summary["active_breaches"] == 2
        assert summary["total_breaches_24h"] >= 2

    def test_component_details_shape_with_real_data(self, tracker):
        _record_many(tracker, "api", 50, success=True, duration_ms=20.0)

        details = tracker.get_component_sla_details("api")

        assert details["component_name"] == "api"
        assert set(details.keys()) == {
            "component_name",
            "current_metrics",
            "sla_thresholds",
            "compliance",
            "breaches",
        }
        current = details["current_metrics"]
        assert current["availability"] == 100.0
        assert current["response_time_p50"] == 20.0
        assert current["status"] == "meeting"
        assert details["sla_thresholds"]["min_availability"] == 99.9
        assert set(details["sla_thresholds"]) == {
            "min_availability",
            "max_error_rate",
            "min_throughput",
            "alert_threshold",
        }, "no latency budget is published, because none exists (#1523)"
        assert details["compliance"] == {
            "availability_compliance": True,
            "error_rate_compliance": True,
        }
        assert details["breaches"]["active_breaches"] == []
        assert details["breaches"]["recent_breaches"] == []
        assert details["breaches"]["total_breaches_7d"] == 0

    def test_component_details_unknown_component(self, tracker):
        details = tracker.get_component_sla_details("does_not_exist")
        assert "error" in details
