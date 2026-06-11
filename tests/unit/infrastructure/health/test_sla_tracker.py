"""Unit tests for the observation-based SLA tracker.

The tracker computes SLA metrics exclusively from real recorded request
observations (no simulation). These tests verify:

- UNKNOWN status with zeroed values when no observations exist
- Correct availability / nearest-rank percentiles / error rate over a window
- Breach detection (availability + error rate), counting, and recovery
- Time-window exclusion of old observations
- Throughput is informational only (never a breach condition)
- Breach severity for total outage (actual_value == 0) does not divide by zero
- Prometheus gauge publishing runs cleanly with NoOp shims
- Summary / detail report shapes
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.infrastructure.health.sla_tracker import (
    SLAStatus,
    SLAThresholds,
    SLATracker,
)


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
                max_response_time_p95=10_000.0,
                max_response_time_p99=20_000.0,
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
class TestBreachSeverity:
    """Severity calculation handles edge values safely."""

    def test_zero_actual_value_is_critical_without_zero_division(self, tracker):
        # Total outage: availability actual_value == 0 must not raise
        severity = tracker._determine_breach_severity("availability", 0.0, 99.9)
        assert severity == "critical"

    def test_higher_is_worse_metric_severity(self, tracker):
        assert tracker._determine_breach_severity("error_rate", 2.0, 1.0) == "critical"
        assert tracker._determine_breach_severity("error_rate", 1.6, 1.0) == "high"
        assert (
            tracker._determine_breach_severity("response_time_p95", 1.25, 1.0)
            == "medium"
        )
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
        # api is the only component with data -> best performer
        assert summary["best_performing_component"] == "api"
        assert summary["worst_performing_component"] is not None
        assert summary["worst_performing_component"] != "api"
        assert summary["active_breaches"] == 0
        assert isinstance(summary["overall_sla"], float)

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
        assert details["compliance"]["availability_compliance"] is True
        assert details["compliance"]["error_rate_compliance"] is True
        assert details["breaches"]["active_breaches"] == []
        assert details["breaches"]["recent_breaches"] == []
        assert details["breaches"]["total_breaches_7d"] == 0

    def test_component_details_unknown_component(self, tracker):
        details = tracker.get_component_sla_details("does_not_exist")
        assert "error" in details
