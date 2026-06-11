"""
SLA Tracker

Tracks and reports Service Level Agreement metrics for FaultMaven components
with configurable thresholds and alerting capabilities.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

# Bound per-component memory: at a sustained 100 req/min this covers >24h,
# which is the widest SLA window we calculate over.
_MAX_OBSERVATIONS_PER_COMPONENT = 200_000


class SLAStatus(Enum):
    """SLA compliance status."""

    MEETING = "meeting"
    AT_RISK = "at_risk"
    BREACHED = "breached"
    UNKNOWN = "unknown"


@dataclass
class SLAMetrics:
    """SLA metrics for a component or service."""

    component_name: str
    availability_percentage: float
    response_time_p50: float
    response_time_p95: float
    response_time_p99: float
    error_rate_percentage: float
    throughput_per_minute: float
    status: SLAStatus
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    breaches_24h: int = 0
    time_to_recovery_minutes: Optional[float] = None


@dataclass
class SLAThresholds:
    """SLA thresholds for a component."""

    component_name: str
    min_availability: float = 99.9  # Percentage
    max_response_time_p95: float = 1000.0  # Milliseconds
    max_response_time_p99: float = 2000.0  # Milliseconds
    max_error_rate: float = 1.0  # Percentage
    min_throughput: float = 10.0  # Requests per minute
    alert_threshold: float = 95.0  # Percentage of SLA before alerting


@dataclass
class SLABreach:
    """Represents an SLA breach event."""

    component_name: str
    metric_type: str  # "availability", "response_time", "error_rate", etc.
    threshold_value: float
    actual_value: float
    breach_start: datetime
    breach_end: Optional[datetime] = None
    duration_minutes: Optional[float] = None
    severity: str = "medium"  # "low", "medium", "high", "critical"


class SLATracker:
    """Tracks SLA metrics and compliance for components."""

    def __init__(self):
        """Initialize SLA tracker."""
        self.logger = logging.getLogger(__name__)
        self.component_thresholds: Dict[str, SLAThresholds] = {}
        self.component_metrics: Dict[str, SLAMetrics] = {}
        self.sla_history: Dict[str, List[Tuple[datetime, SLAMetrics]]] = {}
        self.active_breaches: Dict[str, List[SLABreach]] = {}
        self.breach_history: List[SLABreach] = []
        # Raw request observations per component: (timestamp, duration_ms, success).
        # This is the single data source for all SLA calculations.
        self._observations: Dict[str, Deque[Tuple[datetime, float, bool]]] = {}
        self._initialize_default_thresholds()

    def _initialize_default_thresholds(self) -> None:
        """Initialize default SLA thresholds for FaultMaven components."""
        default_thresholds = {
            "api": SLAThresholds(
                component_name="api",
                min_availability=99.9,
                max_response_time_p95=200.0,
                max_response_time_p99=500.0,
                max_error_rate=0.5,
                min_throughput=100.0,
            ),
            "llm_provider": SLAThresholds(
                component_name="llm_provider",
                min_availability=99.5,
                max_response_time_p95=2000.0,
                max_response_time_p99=5000.0,
                max_error_rate=2.0,
                min_throughput=20.0,
            ),
            "database": SLAThresholds(
                component_name="database",
                min_availability=99.95,
                max_response_time_p95=100.0,
                max_response_time_p99=200.0,
                max_error_rate=0.1,
                min_throughput=500.0,
            ),
            "knowledge_base": SLAThresholds(
                component_name="knowledge_base",
                min_availability=99.0,
                max_response_time_p95=500.0,
                max_response_time_p99=1000.0,
                max_error_rate=1.0,
                min_throughput=50.0,
            ),
            "session_store": SLAThresholds(
                component_name="session_store",
                min_availability=99.9,
                max_response_time_p95=50.0,
                max_response_time_p99=100.0,
                max_error_rate=0.5,
                min_throughput=200.0,
            ),
        }

        for component, thresholds in default_thresholds.items():
            self.set_sla_thresholds(component, thresholds)

    def set_sla_thresholds(
        self, component_name: str, thresholds: SLAThresholds
    ) -> None:
        """Set SLA thresholds for a component.

        Args:
            component_name: Name of the component
            thresholds: SLA thresholds configuration
        """
        self.component_thresholds[component_name] = thresholds
        self.logger.info(f"Set SLA thresholds for component: {component_name}")

    def record_request_metrics(
        self,
        component_name: str,
        response_time_ms: float,
        success: bool,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Record metrics for a single request.

        Args:
            component_name: Name of the component that handled the request
            response_time_ms: Response time in milliseconds
            success: Whether the request was successful
            timestamp: When the request occurred (defaults to now)
        """
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        if component_name not in self._observations:
            self._observations[component_name] = deque(
                maxlen=_MAX_OBSERVATIONS_PER_COMPONENT
            )
        self._observations[component_name].append(
            (timestamp, response_time_ms, success)
        )

    def calculate_sla_metrics(
        self, component_name: str, time_window_hours: int = 24
    ) -> SLAMetrics:
        """Calculate current SLA metrics for a component from recorded observations.

        Components with no observations in the window return UNKNOWN status
        with zeroed values — never fabricated data — and are excluded from
        breach detection.

        Args:
            component_name: Name of the component
            time_window_hours: Time window to calculate metrics over

        Returns:
            Current SLA metrics for the component
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=time_window_hours)
        window = [
            (ts, duration_ms, success)
            for ts, duration_ms, success in self._observations.get(component_name, ())
            if ts >= cutoff
        ]

        if not window:
            metrics = SLAMetrics(
                component_name=component_name,
                availability_percentage=0.0,
                response_time_p50=0.0,
                response_time_p95=0.0,
                response_time_p99=0.0,
                error_rate_percentage=0.0,
                throughput_per_minute=0.0,
                status=SLAStatus.UNKNOWN,
            )
            self.component_metrics[component_name] = metrics
            return metrics

        durations = sorted(d for _, d, _ in window)
        successes = sum(1 for _, _, ok in window if ok)
        total = len(window)

        availability = (successes / total) * 100.0
        # Observed span, floored at one minute so a burst doesn't inflate rate
        span_minutes = max((window[-1][0] - window[0][0]).total_seconds() / 60.0, 1.0)

        metrics = SLAMetrics(
            component_name=component_name,
            availability_percentage=availability,
            response_time_p50=self._percentile(durations, 0.50),
            response_time_p95=self._percentile(durations, 0.95),
            response_time_p99=self._percentile(durations, 0.99),
            error_rate_percentage=100.0 - availability,
            throughput_per_minute=total / span_minutes,
            status=SLAStatus.MEETING,
            breaches_24h=self._count_breaches_24h(component_name),
        )

        # Determine SLA status
        metrics.status = self._determine_sla_status(component_name, metrics)

        # Update stored metrics
        self.component_metrics[component_name] = metrics

        # Record in history
        self._record_metrics_history(component_name, metrics)

        # Check for SLA breaches
        self._check_sla_breaches(component_name, metrics)

        # breaches_24h may have changed if a breach just started/ended
        metrics.breaches_24h = self._count_breaches_24h(component_name)

        return metrics

    @staticmethod
    def _percentile(sorted_values: List[float], fraction: float) -> float:
        """Nearest-rank percentile over an already-sorted list."""
        if not sorted_values:
            return 0.0
        rank = max(int(round(fraction * len(sorted_values) + 0.5)) - 1, 0)
        return sorted_values[min(rank, len(sorted_values) - 1)]

    def _count_breaches_24h(self, component_name: str) -> int:
        """Count breaches (active + ended) that started in the last 24h."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        ended = sum(
            1
            for breach in self.breach_history
            if breach.component_name == component_name and breach.breach_start >= cutoff
        )
        active = sum(
            1
            for breach in self.active_breaches.get(component_name, [])
            if breach.breach_start >= cutoff
        )
        return ended + active

    def _determine_sla_status(
        self, component_name: str, metrics: SLAMetrics
    ) -> SLAStatus:
        """Determine SLA compliance status based on thresholds.

        Args:
            component_name: Name of the component
            metrics: Current metrics for the component

        Returns:
            SLA compliance status
        """
        if component_name not in self.component_thresholds:
            return SLAStatus.UNKNOWN

        thresholds = self.component_thresholds[component_name]

        # Check for breaches
        breaches = []

        if metrics.availability_percentage < thresholds.min_availability:
            breaches.append("availability")

        if metrics.response_time_p95 > thresholds.max_response_time_p95:
            breaches.append("response_time_p95")

        if metrics.response_time_p99 > thresholds.max_response_time_p99:
            breaches.append("response_time_p99")

        if metrics.error_rate_percentage > thresholds.max_error_rate:
            breaches.append("error_rate")

        # Throughput is deliberately NOT a breach condition: a quiet system
        # (low demand) is not violating its SLA. min_throughput stays in the
        # thresholds for display/capacity context only.

        # Determine status
        if breaches:
            return SLAStatus.BREACHED

        # Check if at risk (within alert threshold)
        availability_margin = (
            metrics.availability_percentage / thresholds.min_availability
        ) * 100
        response_time_margin = (
            (thresholds.max_response_time_p95 / metrics.response_time_p95) * 100
            if metrics.response_time_p95 > 0
            else 100.0
        )

        if (
            availability_margin < thresholds.alert_threshold
            or response_time_margin < thresholds.alert_threshold
        ):
            return SLAStatus.AT_RISK

        return SLAStatus.MEETING

    def _record_metrics_history(self, component_name: str, metrics: SLAMetrics) -> None:
        """Record metrics in historical data."""
        if component_name not in self.sla_history:
            self.sla_history[component_name] = []

        # Add new record
        self.sla_history[component_name].append((datetime.now(timezone.utc), metrics))

        # Keep only last 7 days of history
        cutoff_time = datetime.now(timezone.utc) - timedelta(days=7)
        self.sla_history[component_name] = [
            record
            for record in self.sla_history[component_name]
            if record[0] >= cutoff_time
        ]

    def _check_sla_breaches(self, component_name: str, metrics: SLAMetrics) -> None:
        """Check for and record SLA breaches."""
        if component_name not in self.component_thresholds:
            return

        thresholds = self.component_thresholds[component_name]
        current_time = datetime.now(timezone.utc)

        # Initialize active breaches for component if not exists
        if component_name not in self.active_breaches:
            self.active_breaches[component_name] = []

        # Check each metric for breaches
        breach_checks = [
            (
                "availability",
                metrics.availability_percentage,
                thresholds.min_availability,
                "less_than",
            ),
            (
                "response_time_p95",
                metrics.response_time_p95,
                thresholds.max_response_time_p95,
                "greater_than",
            ),
            (
                "response_time_p99",
                metrics.response_time_p99,
                thresholds.max_response_time_p99,
                "greater_than",
            ),
            (
                "error_rate",
                metrics.error_rate_percentage,
                thresholds.max_error_rate,
                "greater_than",
            ),
            # throughput intentionally excluded — see _determine_sla_status
        ]

        for metric_type, actual_value, threshold_value, comparison in breach_checks:
            is_breach = (
                comparison == "greater_than" and actual_value > threshold_value
            ) or (comparison == "less_than" and actual_value < threshold_value)

            # Find existing active breach for this metric
            existing_breach = None
            for breach in self.active_breaches[component_name]:
                if breach.metric_type == metric_type and breach.breach_end is None:
                    existing_breach = breach
                    break

            if is_breach and not existing_breach:
                # Start new breach
                severity = self._determine_breach_severity(
                    metric_type, actual_value, threshold_value
                )
                new_breach = SLABreach(
                    component_name=component_name,
                    metric_type=metric_type,
                    threshold_value=threshold_value,
                    actual_value=actual_value,
                    breach_start=current_time,
                    severity=severity,
                )
                self.active_breaches[component_name].append(new_breach)
                self.logger.warning(
                    f"SLA breach started for {component_name}.{metric_type}: {actual_value} vs {threshold_value}"
                )

            elif not is_breach and existing_breach:
                # End existing breach
                existing_breach.breach_end = current_time
                existing_breach.duration_minutes = (
                    current_time - existing_breach.breach_start
                ).total_seconds() / 60

                # Move to breach history
                self.breach_history.append(existing_breach)
                self.active_breaches[component_name].remove(existing_breach)

                self.logger.info(
                    f"SLA breach ended for {component_name}.{metric_type} after {existing_breach.duration_minutes:.2f} minutes"
                )

    def _determine_breach_severity(
        self, metric_type: str, actual_value: float, threshold_value: float
    ) -> str:
        """Determine the severity of an SLA breach.

        Args:
            metric_type: Type of metric that was breached
            actual_value: Actual measured value
            threshold_value: SLA threshold that was breached

        Returns:
            Severity level: "low", "medium", "high", or "critical"
        """
        # Calculate how far the breach is from the threshold
        if metric_type in ["response_time_p95", "response_time_p99", "error_rate"]:
            # For metrics where higher is worse
            ratio = actual_value / threshold_value if threshold_value > 0 else 2.0
        else:
            # For metrics where lower is worse (availability); an actual of 0
            # (total outage) is the worst possible breach
            ratio = threshold_value / actual_value if actual_value > 0 else 2.0

        if ratio >= 2.0:
            return "critical"
        elif ratio >= 1.5:
            return "high"
        elif ratio >= 1.2:
            return "medium"
        else:
            return "low"

    def get_sla_summary(self, time_window_hours: int = 24) -> Dict[str, Any]:
        """Get SLA summary for all components.

        Args:
            time_window_hours: Time window to calculate summary over

        Returns:
            Dictionary with SLA summary information
        """
        summary = {
            "overall_sla": 0.0,
            "components": {},
            "active_breaches": 0,
            "total_breaches_24h": 0,
            "worst_performing_component": None,
            "best_performing_component": None,
        }

        if not self.component_thresholds:
            return summary

        component_slas = []
        worst_sla = 100.0
        best_sla = 0.0
        worst_component = None
        best_component = None

        for component_name in self.component_thresholds.keys():
            metrics = self.calculate_sla_metrics(component_name, time_window_hours)

            summary["components"][component_name] = {
                "sla": metrics.availability_percentage,
                "status": metrics.status.value,
                "response_time_p95": metrics.response_time_p95,
                "error_rate": metrics.error_rate_percentage,
                "breaches_24h": metrics.breaches_24h,
            }

            component_slas.append(metrics.availability_percentage)
            summary["total_breaches_24h"] += metrics.breaches_24h

            if metrics.availability_percentage < worst_sla:
                worst_sla = metrics.availability_percentage
                worst_component = component_name

            if metrics.availability_percentage > best_sla:
                best_sla = metrics.availability_percentage
                best_component = component_name

        # Calculate overall SLA as average of component SLAs
        if component_slas:
            summary["overall_sla"] = round(sum(component_slas) / len(component_slas), 2)

        summary["worst_performing_component"] = worst_component
        summary["best_performing_component"] = best_component

        # Count active breaches
        summary["active_breaches"] = sum(
            len(breaches) for breaches in self.active_breaches.values()
        )

        return summary

    def get_component_sla_details(self, component_name: str) -> Dict[str, Any]:
        """Get detailed SLA information for a specific component.

        Args:
            component_name: Name of the component

        Returns:
            Detailed SLA information for the component
        """
        if component_name not in self.component_thresholds:
            return {"error": f"Component {component_name} not found"}

        thresholds = self.component_thresholds[component_name]
        metrics = self.calculate_sla_metrics(component_name)

        # Get recent breach history
        recent_breaches = [
            {
                "metric_type": breach.metric_type,
                "severity": breach.severity,
                "start_time": breach.breach_start.isoformat(),
                "end_time": (
                    breach.breach_end.isoformat() if breach.breach_end else None
                ),
                "duration_minutes": breach.duration_minutes,
                "threshold_value": breach.threshold_value,
                "actual_value": breach.actual_value,
            }
            for breach in self.breach_history
            if breach.component_name == component_name
            and breach.breach_start >= datetime.now(timezone.utc) - timedelta(days=7)
        ]

        # Get active breaches
        active_breaches = [
            {
                "metric_type": breach.metric_type,
                "severity": breach.severity,
                "start_time": breach.breach_start.isoformat(),
                "threshold_value": breach.threshold_value,
                "actual_value": breach.actual_value,
                "duration_minutes": (
                    datetime.now(timezone.utc) - breach.breach_start
                ).total_seconds()
                / 60,
            }
            for breach in self.active_breaches.get(component_name, [])
        ]

        return {
            "component_name": component_name,
            "current_metrics": {
                "availability": metrics.availability_percentage,
                "response_time_p50": metrics.response_time_p50,
                "response_time_p95": metrics.response_time_p95,
                "response_time_p99": metrics.response_time_p99,
                "error_rate": metrics.error_rate_percentage,
                "throughput": metrics.throughput_per_minute,
                "status": metrics.status.value,
            },
            "sla_thresholds": {
                "min_availability": thresholds.min_availability,
                "max_response_time_p95": thresholds.max_response_time_p95,
                "max_response_time_p99": thresholds.max_response_time_p99,
                "max_error_rate": thresholds.max_error_rate,
                "min_throughput": thresholds.min_throughput,
                "alert_threshold": thresholds.alert_threshold,
            },
            "compliance": {
                "availability_compliance": metrics.availability_percentage
                >= thresholds.min_availability,
                "response_time_compliance": metrics.response_time_p95
                <= thresholds.max_response_time_p95,
                "error_rate_compliance": metrics.error_rate_percentage
                <= thresholds.max_error_rate,
                "throughput_compliance": metrics.throughput_per_minute
                >= thresholds.min_throughput,
            },
            "breaches": {
                "active_breaches": active_breaches,
                "recent_breaches": recent_breaches[-10:],  # Last 10 breaches
                "total_breaches_7d": len(recent_breaches),
            },
        }

    # Mapping for the sla_status gauge: higher is healthier (alert on < 3)
    _STATUS_GAUGE_VALUES = {
        SLAStatus.MEETING: 3,
        SLAStatus.AT_RISK: 2,
        SLAStatus.BREACHED: 1,
        SLAStatus.UNKNOWN: 0,
    }

    def update_prometheus_gauges(self) -> None:
        """Recompute SLA metrics and publish them as Prometheus gauges.

        Registered as a /metrics scrape hook (see main.py) so the gauges are
        fresh at every Prometheus scrape and /health/sla becomes alertable.
        No-ops when metrics are disabled (shim returns no-op metrics).
        """
        from faultmaven.infrastructure.shims import (
            sla_active_breaches,
            sla_availability_ratio,
            sla_error_rate_ratio,
            sla_response_time_p95_seconds,
            sla_status,
        )

        for component_name in self.component_thresholds.keys():
            metrics = self.calculate_sla_metrics(component_name)
            sla_status.labels(component=component_name).set(
                self._STATUS_GAUGE_VALUES[metrics.status]
            )
            sla_availability_ratio.labels(component=component_name).set(
                metrics.availability_percentage / 100.0
            )
            sla_response_time_p95_seconds.labels(component=component_name).set(
                metrics.response_time_p95 / 1000.0
            )
            sla_error_rate_ratio.labels(component=component_name).set(
                metrics.error_rate_percentage / 100.0
            )
            sla_active_breaches.labels(component=component_name).set(
                len(self.active_breaches.get(component_name, []))
            )


# Global SLA tracker instance
sla_tracker = SLATracker()
