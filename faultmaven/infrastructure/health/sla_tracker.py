"""
SLA Tracker

Tracks and reports Service Level Agreement metrics for FaultMaven components
with configurable thresholds and alerting capabilities.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging
import statistics


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
                min_throughput=100.0
            ),
            "llm_provider": SLAThresholds(
                component_name="llm_provider",
                min_availability=99.5,
                max_response_time_p95=2000.0,
                max_response_time_p99=5000.0,
                max_error_rate=2.0,
                min_throughput=20.0
            ),
            "database": SLAThresholds(
                component_name="database",
                min_availability=99.95,
                max_response_time_p95=100.0,
                max_response_time_p99=200.0,
                max_error_rate=0.1,
                min_throughput=500.0
            ),
            "knowledge_base": SLAThresholds(
                component_name="knowledge_base",
                min_availability=99.0,
                max_response_time_p95=500.0,
                max_response_time_p99=1000.0,
                max_error_rate=1.0,
                min_throughput=50.0
            ),
            "session_store": SLAThresholds(
                component_name="session_store",
                min_availability=99.9,
                max_response_time_p95=50.0,
                max_response_time_p99=100.0,
                max_error_rate=0.5,
                min_throughput=200.0
            )
        }
        
        for component, thresholds in default_thresholds.items():
            self.set_sla_thresholds(component, thresholds)
    
    def set_sla_thresholds(self, component_name: str, thresholds: SLAThresholds) -> None:
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
        timestamp: Optional[datetime] = None
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
        
        # Initialize component metrics if not exists
        if component_name not in self.component_metrics:
            self.component_metrics[component_name] = SLAMetrics(
                component_name=component_name,
                availability_percentage=100.0,
                response_time_p50=response_time_ms,
                response_time_p95=response_time_ms,
                response_time_p99=response_time_ms,
                error_rate_percentage=0.0,
                throughput_per_minute=0.0,
                status=SLAStatus.MEETING
            )
        
        # Update metrics will be called periodically to recalculate all metrics
        # For now, we'll update basic tracking
        self.logger.debug(f"Recorded request for {component_name}: {response_time_ms}ms, success={success}")
    
    def calculate_sla_metrics(
        self,
        component_name: str,
        time_window_hours: int = 24
    ) -> SLAMetrics:
        """Calculate current SLA metrics for a component.
        
        Args:
            component_name: Name of the component
            time_window_hours: Time window to calculate metrics over
            
        Returns:
            Current SLA metrics for the component
        """
        # In a real implementation, this would pull data from monitoring/metrics store
        # For now, we'll simulate realistic metrics
        
        # Simulate data based on component type
        if component_name == "api":
            metrics = self._simulate_api_metrics()
        elif component_name == "llm_provider":
            metrics = self._simulate_llm_metrics()
        elif component_name == "database":
            metrics = self._simulate_database_metrics()
        elif component_name == "knowledge_base":
            metrics = self._simulate_knowledge_base_metrics()
        elif component_name == "session_store":
            metrics = self._simulate_session_store_metrics()
        else:
            metrics = self._simulate_generic_metrics(component_name)
        
        # Determine SLA status
        metrics.status = self._determine_sla_status(component_name, metrics)
        
        # Update stored metrics
        self.component_metrics[component_name] = metrics
        
        # Record in history
        self._record_metrics_history(component_name, metrics)
        
        # Check for SLA breaches
        self._check_sla_breaches(component_name, metrics)
        
        return metrics
    
    def _simulate_api_metrics(self) -> SLAMetrics:
        """Simulate API layer metrics."""
        import random
        
        # Simulate good performance with occasional degradation
        base_availability = 99.95
        availability_variance = random.uniform(-0.1, 0.05)
        
        return SLAMetrics(
            component_name="api",
            availability_percentage=base_availability + availability_variance,
            response_time_p50=random.uniform(80, 120),
            response_time_p95=random.uniform(150, 250),
            response_time_p99=random.uniform(300, 500),
            error_rate_percentage=random.uniform(0.1, 0.8),
            throughput_per_minute=random.uniform(150, 300),
            status=SLAStatus.MEETING,
            breaches_24h=random.randint(0, 2)
        )
    
    def _simulate_llm_metrics(self) -> SLAMetrics:
        """Simulate LLM provider metrics."""
        import random
        
        # LLM providers can be more variable
        base_availability = 99.2
        availability_variance = random.uniform(-0.5, 0.3)
        
        return SLAMetrics(
            component_name="llm_provider",
            availability_percentage=base_availability + availability_variance,
            response_time_p50=random.uniform(1200, 1800),
            response_time_p95=random.uniform(2500, 4000),
            response_time_p99=random.uniform(5000, 8000),
            error_rate_percentage=random.uniform(0.5, 3.0),
            throughput_per_minute=random.uniform(25, 60),
            status=SLAStatus.MEETING,
            breaches_24h=random.randint(1, 5)
        )
    
    def _simulate_database_metrics(self) -> SLAMetrics:
        """Simulate database metrics."""
        import random
        
        # Databases should be very reliable
        base_availability = 99.98
        availability_variance = random.uniform(-0.02, 0.01)
        
        return SLAMetrics(
            component_name="database",
            availability_percentage=base_availability + availability_variance,
            response_time_p50=random.uniform(15, 35),
            response_time_p95=random.uniform(60, 120),
            response_time_p99=random.uniform(150, 250),
            error_rate_percentage=random.uniform(0.01, 0.2),
            throughput_per_minute=random.uniform(800, 1200),
            status=SLAStatus.MEETING,
            breaches_24h=random.randint(0, 1)
        )
    
    def _simulate_knowledge_base_metrics(self) -> SLAMetrics:
        """Simulate knowledge base metrics."""
        import random
        
        base_availability = 98.8
        availability_variance = random.uniform(-0.3, 0.2)
        
        return SLAMetrics(
            component_name="knowledge_base",
            availability_percentage=base_availability + availability_variance,
            response_time_p50=random.uniform(200, 400),
            response_time_p95=random.uniform(600, 1000),
            response_time_p99=random.uniform(1200, 2000),
            error_rate_percentage=random.uniform(0.3, 1.5),
            throughput_per_minute=random.uniform(80, 150),
            status=SLAStatus.MEETING,
            breaches_24h=random.randint(0, 3)
        )
    
    def _simulate_session_store_metrics(self) -> SLAMetrics:
        """Simulate session store (Redis) metrics."""
        import random
        
        # Redis should be very fast and reliable
        base_availability = 99.95
        availability_variance = random.uniform(-0.05, 0.02)
        
        return SLAMetrics(
            component_name="session_store",
            availability_percentage=base_availability + availability_variance,
            response_time_p50=random.uniform(5, 15),
            response_time_p95=random.uniform(20, 50),
            response_time_p99=random.uniform(60, 120),
            error_rate_percentage=random.uniform(0.01, 0.3),
            throughput_per_minute=random.uniform(400, 800),
            status=SLAStatus.MEETING,
            breaches_24h=random.randint(0, 1)
        )
    
    def _simulate_generic_metrics(self, component_name: str) -> SLAMetrics:
        """Simulate generic component metrics."""
        import random
        
        base_availability = 99.0
        availability_variance = random.uniform(-1.0, 0.5)
        
        return SLAMetrics(
            component_name=component_name,
            availability_percentage=base_availability + availability_variance,
            response_time_p50=random.uniform(100, 300),
            response_time_p95=random.uniform(400, 800),
            response_time_p99=random.uniform(1000, 2000),
            error_rate_percentage=random.uniform(0.5, 2.0),
            throughput_per_minute=random.uniform(50, 200),
            status=SLAStatus.MEETING,
            breaches_24h=random.randint(0, 4)
        )
    
    def _determine_sla_status(self, component_name: str, metrics: SLAMetrics) -> SLAStatus:
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
        
        if metrics.throughput_per_minute < thresholds.min_throughput:
            breaches.append("throughput")
        
        # Determine status
        if breaches:
            return SLAStatus.BREACHED
        
        # Check if at risk (within alert threshold)
        availability_margin = (metrics.availability_percentage / thresholds.min_availability) * 100
        response_time_margin = (thresholds.max_response_time_p95 / metrics.response_time_p95) * 100
        
        if availability_margin < thresholds.alert_threshold or response_time_margin < thresholds.alert_threshold:
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
            record for record in self.sla_history[component_name]
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
            ("availability", metrics.availability_percentage, thresholds.min_availability, "less_than"),
            ("response_time_p95", metrics.response_time_p95, thresholds.max_response_time_p95, "greater_than"),
            ("response_time_p99", metrics.response_time_p99, thresholds.max_response_time_p99, "greater_than"),
            ("error_rate", metrics.error_rate_percentage, thresholds.max_error_rate, "greater_than"),
            ("throughput", metrics.throughput_per_minute, thresholds.min_throughput, "less_than")
        ]
        
        for metric_type, actual_value, threshold_value, comparison in breach_checks:
            is_breach = (
                (comparison == "greater_than" and actual_value > threshold_value) or
                (comparison == "less_than" and actual_value < threshold_value)
            )
            
            # Find existing active breach for this metric
            existing_breach = None
            for breach in self.active_breaches[component_name]:
                if breach.metric_type == metric_type and breach.breach_end is None:
                    existing_breach = breach
                    break
            
            if is_breach and not existing_breach:
                # Start new breach
                severity = self._determine_breach_severity(metric_type, actual_value, threshold_value)
                new_breach = SLABreach(
                    component_name=component_name,
                    metric_type=metric_type,
                    threshold_value=threshold_value,
                    actual_value=actual_value,
                    breach_start=current_time,
                    severity=severity
                )
                self.active_breaches[component_name].append(new_breach)
                self.logger.warning(f"SLA breach started for {component_name}.{metric_type}: {actual_value} vs {threshold_value}")
            
            elif not is_breach and existing_breach:
                # End existing breach
                existing_breach.breach_end = current_time
                existing_breach.duration_minutes = (
                    current_time - existing_breach.breach_start
                ).total_seconds() / 60
                
                # Move to breach history
                self.breach_history.append(existing_breach)
                self.active_breaches[component_name].remove(existing_breach)
                
                self.logger.info(f"SLA breach ended for {component_name}.{metric_type} after {existing_breach.duration_minutes:.2f} minutes")
    
    def _determine_breach_severity(self, metric_type: str, actual_value: float, threshold_value: float) -> str:
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
            ratio = actual_value / threshold_value
        else:
            # For metrics where lower is worse (availability, throughput)
            ratio = threshold_value / actual_value
        
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
            "best_performing_component": None
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
                "breaches_24h": metrics.breaches_24h
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
                "end_time": breach.breach_end.isoformat() if breach.breach_end else None,
                "duration_minutes": breach.duration_minutes,
                "threshold_value": breach.threshold_value,
                "actual_value": breach.actual_value
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
                "duration_minutes": (datetime.now(timezone.utc) - breach.breach_start).total_seconds() / 60
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
                "status": metrics.status.value
            },
            "sla_thresholds": {
                "min_availability": thresholds.min_availability,
                "max_response_time_p95": thresholds.max_response_time_p95,
                "max_response_time_p99": thresholds.max_response_time_p99,
                "max_error_rate": thresholds.max_error_rate,
                "min_throughput": thresholds.min_throughput,
                "alert_threshold": thresholds.alert_threshold
            },
            "compliance": {
                "availability_compliance": metrics.availability_percentage >= thresholds.min_availability,
                "response_time_compliance": metrics.response_time_p95 <= thresholds.max_response_time_p95,
                "error_rate_compliance": metrics.error_rate_percentage <= thresholds.max_error_rate,
                "throughput_compliance": metrics.throughput_per_minute >= thresholds.min_throughput
            },
            "breaches": {
                "active_breaches": active_breaches,
                "recent_breaches": recent_breaches[-10:],  # Last 10 breaches
                "total_breaches_7d": len(recent_breaches)
            }
        }


# Global SLA tracker instance
sla_tracker = SLATracker()