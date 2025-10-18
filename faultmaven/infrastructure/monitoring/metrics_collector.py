"""
Metrics Collector

Collects and aggregates performance metrics from various components
with configurable alerting thresholds and dashboard data endpoints.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple, Callable
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging
import time
import statistics
import asyncio
from collections import defaultdict, deque


class MetricType(Enum):
    """Types of metrics that can be collected."""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"


@dataclass
class PerformanceMetrics:
    """Container for performance metrics data."""
    timestamp: datetime
    component: str
    operation: str
    duration_ms: float
    success: bool
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class MetricSample:
    """Individual metric sample."""
    value: float
    timestamp: datetime
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class AggregatedMetrics:
    """Aggregated metrics over a time window."""
    metric_name: str
    component: str
    count: int
    sum_value: float
    avg_value: float
    min_value: float
    max_value: float
    p50_value: float
    p95_value: float
    p99_value: float
    start_time: datetime
    end_time: datetime
    tags: Dict[str, str] = field(default_factory=dict)


class MetricsCollector:
    """Collects and aggregates performance metrics with alerting capabilities."""
    
    def __init__(self, max_samples: int = 10000, retention_hours: int = 24):
        """Initialize metrics collector.
        
        Args:
            max_samples: Maximum number of samples to keep in memory per metric
            retention_hours: How long to retain metrics data
        """
        self.logger = logging.getLogger(__name__)
        self.max_samples = max_samples
        self.retention_hours = retention_hours
        
        # Storage for raw metrics
        self.metrics: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_samples))
        
        # Aggregated metrics cache
        self.aggregated_cache: Dict[str, AggregatedMetrics] = {}
        self.last_aggregation: Dict[str, datetime] = {}
        
        # Alert thresholds
        self.alert_thresholds: Dict[str, Dict[str, float]] = {}
        self.alert_callbacks: List[Callable] = []
        
        # Performance tracking
        self.start_times: Dict[str, float] = {}
        
        # Dashboard data
        self.dashboard_data: Dict[str, Any] = {}
        
        self._initialize_default_thresholds()
    
    def _initialize_default_thresholds(self) -> None:
        """Initialize default alert thresholds for common metrics."""
        self.alert_thresholds = {
            "api.request_duration": {
                "p95_ms": 500.0,
                "p99_ms": 1000.0,
                "avg_ms": 200.0
            },
            "api.error_rate": {
                "percentage": 5.0
            },
            "llm.request_duration": {
                "p95_ms": 3000.0,
                "p99_ms": 8000.0,
                "avg_ms": 1500.0
            },
            "database.query_duration": {
                "p95_ms": 100.0,
                "p99_ms": 200.0,
                "avg_ms": 50.0
            },
            "memory.usage": {
                "percentage": 85.0
            },
            "cpu.usage": {
                "percentage": 80.0
            }
        }
    
    def start_timer(self, operation_id: str) -> str:
        """Start timing an operation.
        
        Args:
            operation_id: Unique identifier for the operation
            
        Returns:
            Timer ID for stopping the timer
        """
        timer_id = f"{operation_id}_{time.time()}"
        self.start_times[timer_id] = time.time()
        return timer_id
    
    def stop_timer(
        self,
        timer_id: str,
        component: str,
        operation: str,
        success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None
    ) -> float:
        """Stop timing an operation and record metrics.
        
        Args:
            timer_id: Timer ID from start_timer
            component: Component name
            operation: Operation name
            success: Whether the operation was successful
            metadata: Additional metadata
            tags: Tags for the metric
            
        Returns:
            Duration in milliseconds
        """
        if timer_id not in self.start_times:
            self.logger.warning(f"Timer ID not found: {timer_id}")
            return 0.0
        
        duration_ms = (time.time() - self.start_times[timer_id]) * 1000
        del self.start_times[timer_id]
        
        # Record the performance metric
        self.record_performance_metric(
            component=component,
            operation=operation,
            duration_ms=duration_ms,
            success=success,
            metadata=metadata,
            tags=tags
        )
        
        return duration_ms
    
    def record_performance_metric(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None
    ) -> None:
        """Record a performance metric.
        
        Args:
            component: Component name (e.g., 'api', 'llm', 'database')
            operation: Operation name (e.g., 'request', 'query', 'inference')
            duration_ms: Duration in milliseconds
            success: Whether the operation was successful
            metadata: Additional metadata
            tags: Tags for the metric
        """
        metric = PerformanceMetrics(
            timestamp=datetime.now(timezone.utc),
            component=component,
            operation=operation,
            duration_ms=duration_ms,
            success=success,
            metadata=metadata or {},
            tags=tags or {}
        )
        
        # Store metric
        metric_key = f"{component}.{operation}"
        self.metrics[metric_key].append(metric)
        
        # Record in specialized metric stores
        self._record_duration_metric(metric_key, duration_ms, tags or {})
        self._record_success_metric(metric_key, success, tags or {})
        
        # Clean old metrics
        self._cleanup_old_metrics()
        
        # Check alert thresholds
        self._check_alert_thresholds(metric_key)
        
        self.logger.debug(f"Recorded metric: {metric_key} = {duration_ms}ms, success={success}")
    
    def record_counter_metric(
        self,
        metric_name: str,
        value: float = 1.0,
        tags: Optional[Dict[str, str]] = None
    ) -> None:
        """Record a counter metric.
        
        Args:
            metric_name: Name of the metric
            value: Value to add to the counter
            tags: Tags for the metric
        """
        sample = MetricSample(
            value=value,
            timestamp=datetime.now(timezone.utc),
            tags=tags or {}
        )
        
        self.metrics[metric_name].append(sample)
        self.logger.debug(f"Recorded counter: {metric_name} += {value}")
    
    def record_gauge_metric(
        self,
        metric_name: str,
        value: float,
        tags: Optional[Dict[str, str]] = None
    ) -> None:
        """Record a gauge metric.
        
        Args:
            metric_name: Name of the metric
            value: Current value of the gauge
            tags: Tags for the metric
        """
        sample = MetricSample(
            value=value,
            timestamp=datetime.now(timezone.utc),
            tags=tags or {}
        )
        
        # For gauges, we only keep the most recent values
        gauge_key = f"gauge.{metric_name}"
        if len(self.metrics[gauge_key]) >= 100:
            # Keep only recent samples for gauges
            self.metrics[gauge_key] = deque(list(self.metrics[gauge_key])[-50:], maxlen=self.max_samples)
        
        self.metrics[gauge_key].append(sample)
        self.logger.debug(f"Recorded gauge: {metric_name} = {value}")
    
    def _record_duration_metric(self, metric_key: str, duration_ms: float, tags: Dict[str, str]) -> None:
        """Record duration-specific metric."""
        duration_key = f"duration.{metric_key}"
        sample = MetricSample(
            value=duration_ms,
            timestamp=datetime.now(timezone.utc),
            tags=tags
        )
        self.metrics[duration_key].append(sample)
    
    def _record_success_metric(self, metric_key: str, success: bool, tags: Dict[str, str]) -> None:
        """Record success/failure metric."""
        success_key = f"success.{metric_key}"
        sample = MetricSample(
            value=1.0 if success else 0.0,
            timestamp=datetime.now(timezone.utc),
            tags=tags
        )
        self.metrics[success_key].append(sample)
    
    def _cleanup_old_metrics(self) -> None:
        """Remove metrics older than retention period."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.retention_hours)
        
        for metric_name, samples in self.metrics.items():
            # Remove old samples
            while samples and samples[0].timestamp < cutoff_time:
                samples.popleft()
    
    def get_aggregated_metrics(
        self,
        metric_name: str,
        time_window_minutes: int = 60,
        tags_filter: Optional[Dict[str, str]] = None
    ) -> Optional[AggregatedMetrics]:
        """Get aggregated metrics for a specific metric over a time window.
        
        Args:
            metric_name: Name of the metric to aggregate
            time_window_minutes: Time window for aggregation
            tags_filter: Filter by specific tags
            
        Returns:
            Aggregated metrics or None if no data
        """
        if metric_name not in self.metrics:
            return None
        
        # Check cache first
        cache_key = f"{metric_name}_{time_window_minutes}_{hash(frozenset(tags_filter.items()) if tags_filter else frozenset())}"
        cache_time = self.last_aggregation.get(cache_key)
        
        if cache_time and (datetime.now(timezone.utc) - cache_time).total_seconds() < 60:  # 1-minute cache
            return self.aggregated_cache.get(cache_key)
        
        # Calculate aggregation
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=time_window_minutes)
        
        # Filter samples by time window and tags
        samples = []
        for sample in self.metrics[metric_name]:
            if start_time <= sample.timestamp <= end_time:
                if tags_filter:
                    if all(sample.tags.get(k) == v for k, v in tags_filter.items()):
                        samples.append(sample)
                else:
                    samples.append(sample)
        
        if not samples:
            return None
        
        # Extract values for aggregation
        # Handle both PerformanceMetrics and MetricSample objects
        values = []
        for sample in samples:
            if hasattr(sample, 'value'):
                values.append(sample.value)
            elif hasattr(sample, 'duration_ms'):
                values.append(sample.duration_ms)  # For PerformanceMetrics
            else:
                # Fallback for unknown sample types
                values.append(0.0)
        
        # Calculate aggregations
        aggregated = AggregatedMetrics(
            metric_name=metric_name,
            component=metric_name.split('.')[0] if '.' in metric_name else 'unknown',
            count=len(values),
            sum_value=sum(values),
            avg_value=statistics.mean(values),
            min_value=min(values),
            max_value=max(values),
            p50_value=statistics.median(values),
            p95_value=self._percentile(values, 95),
            p99_value=self._percentile(values, 99),
            start_time=start_time,
            end_time=end_time,
            tags=tags_filter or {}
        )
        
        # Cache result
        self.aggregated_cache[cache_key] = aggregated
        self.last_aggregation[cache_key] = datetime.now(timezone.utc)
        
        return aggregated
    
    def _percentile(self, values: List[float], percentile: float) -> float:
        """Calculate percentile value."""
        if not values:
            return 0.0
        
        sorted_values = sorted(values)
        k = (len(sorted_values) - 1) * (percentile / 100.0)
        floor_k = int(k)
        ceil_k = floor_k + 1
        
        if ceil_k >= len(sorted_values):
            return sorted_values[-1]
        
        if floor_k == ceil_k:
            return sorted_values[floor_k]
        
        # Linear interpolation
        d0 = sorted_values[floor_k] * (ceil_k - k)
        d1 = sorted_values[ceil_k] * (k - floor_k)
        return d0 + d1
    
    def _check_alert_thresholds(self, metric_key: str) -> None:
        """Check if metric has breached alert thresholds."""
        if metric_key not in self.alert_thresholds:
            return
        
        thresholds = self.alert_thresholds[metric_key]
        aggregated = self.get_aggregated_metrics(metric_key, time_window_minutes=5)
        
        if not aggregated:
            return
        
        alerts = []
        
        # Check various threshold types
        if "p95_ms" in thresholds and aggregated.p95_value > thresholds["p95_ms"]:
            alerts.append({
                "metric": metric_key,
                "threshold_type": "p95_ms",
                "threshold_value": thresholds["p95_ms"],
                "actual_value": aggregated.p95_value,
                "severity": "medium"
            })
        
        if "p99_ms" in thresholds and aggregated.p99_value > thresholds["p99_ms"]:
            alerts.append({
                "metric": metric_key,
                "threshold_type": "p99_ms",
                "threshold_value": thresholds["p99_ms"],
                "actual_value": aggregated.p99_value,
                "severity": "high"
            })
        
        if "avg_ms" in thresholds and aggregated.avg_value > thresholds["avg_ms"]:
            alerts.append({
                "metric": metric_key,
                "threshold_type": "avg_ms",
                "threshold_value": thresholds["avg_ms"],
                "actual_value": aggregated.avg_value,
                "severity": "low"
            })
        
        # Trigger alert callbacks
        for alert in alerts:
            self._trigger_alert(alert)
    
    def _trigger_alert(self, alert: Dict[str, Any]) -> None:
        """Trigger alert callbacks."""
        for callback in self.alert_callbacks:
            try:
                callback(alert)
            except Exception as e:
                self.logger.error(f"Alert callback failed: {e}")
        
        self.logger.warning(f"Performance alert: {alert}")
    
    def add_alert_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Add alert callback function.
        
        Args:
            callback: Function to call when alert is triggered
        """
        self.alert_callbacks.append(callback)
    
    def set_alert_threshold(
        self,
        metric_name: str,
        threshold_type: str,
        threshold_value: float
    ) -> None:
        """Set alert threshold for a metric.
        
        Args:
            metric_name: Name of the metric
            threshold_type: Type of threshold (e.g., 'p95_ms', 'avg_ms')
            threshold_value: Threshold value
        """
        if metric_name not in self.alert_thresholds:
            self.alert_thresholds[metric_name] = {}
        
        self.alert_thresholds[metric_name][threshold_type] = threshold_value
        self.logger.info(f"Set alert threshold: {metric_name}.{threshold_type} = {threshold_value}")
    
    def get_dashboard_data(self, time_window_minutes: int = 60) -> Dict[str, Any]:
        """Get real-time performance data for dashboards.
        
        Args:
            time_window_minutes: Time window for dashboard data
            
        Returns:
            Dashboard data structure
        """
        dashboard_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_window_minutes": time_window_minutes,
            "metrics": {},
            "alerts": [],
            "summary": {
                "total_requests": 0,
                "avg_response_time": 0.0,
                "error_rate": 0.0,
                "p95_response_time": 0.0
            }
        }
        
        # Aggregate metrics for each component
        components = set()
        for metric_name in self.metrics.keys():
            if '.' in metric_name and not metric_name.startswith(('duration.', 'success.', 'gauge.')):
                component = metric_name.split('.')[0]
                components.add(component)
        
        total_requests = 0
        total_response_time = 0.0
        total_errors = 0
        p95_times = []
        
        for component in components:
            component_metrics = {}
            
            # Get duration metrics
            duration_key = f"duration.{component}"
            if f"{component}.request" in self.metrics:
                duration_aggregated = self.get_aggregated_metrics(f"duration.{component}.request", time_window_minutes)
                if duration_aggregated:
                    component_metrics["duration"] = {
                        "avg": duration_aggregated.avg_value,
                        "p50": duration_aggregated.p50_value,
                        "p95": duration_aggregated.p95_value,
                        "p99": duration_aggregated.p99_value,
                        "count": duration_aggregated.count
                    }
                    
                    total_requests += duration_aggregated.count
                    total_response_time += duration_aggregated.avg_value * duration_aggregated.count
                    p95_times.append(duration_aggregated.p95_value)
            
            # Get success rate
            success_key = f"success.{component}.request"
            if success_key in self.metrics:
                success_aggregated = self.get_aggregated_metrics(success_key, time_window_minutes)
                if success_aggregated:
                    success_rate = success_aggregated.avg_value * 100
                    error_rate = 100 - success_rate
                    component_metrics["success_rate"] = success_rate
                    component_metrics["error_rate"] = error_rate
                    
                    total_errors += int(success_aggregated.count * (1 - success_aggregated.avg_value))
            
            dashboard_data["metrics"][component] = component_metrics
        
        # Calculate summary statistics
        if total_requests > 0:
            dashboard_data["summary"]["total_requests"] = total_requests
            dashboard_data["summary"]["avg_response_time"] = total_response_time / total_requests
            dashboard_data["summary"]["error_rate"] = (total_errors / total_requests) * 100
            
        if p95_times:
            dashboard_data["summary"]["p95_response_time"] = max(p95_times)
        
        # Add recent alerts (simulate for now)
        recent_alerts = self._get_recent_alerts()
        dashboard_data["alerts"] = recent_alerts
        
        return dashboard_data
    
    def _get_recent_alerts(self) -> List[Dict[str, Any]]:
        """Get recent performance alerts."""
        # In a real implementation, this would fetch from an alert store
        # For now, simulate some alerts based on current metrics
        alerts = []
        
        for metric_name, thresholds in self.alert_thresholds.items():
            aggregated = self.get_aggregated_metrics(metric_name, time_window_minutes=5)
            if aggregated:
                if "p95_ms" in thresholds and aggregated.p95_value > thresholds["p95_ms"]:
                    alerts.append({
                        "metric": metric_name,
                        "threshold_type": "p95_ms",
                        "severity": "medium",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "message": f"{metric_name} P95 response time ({aggregated.p95_value:.1f}ms) exceeds threshold ({thresholds['p95_ms']}ms)"
                    })
        
        return alerts[-10:]  # Return last 10 alerts
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get summary of all collected metrics.
        
        Returns:
            Summary of metrics collection status
        """
        total_samples = sum(len(samples) for samples in self.metrics.values())
        
        # Calculate memory usage (rough estimate)
        estimated_memory_kb = total_samples * 0.1  # Rough estimate: 100 bytes per sample
        
        return {
            "total_metrics": len(self.metrics),
            "total_samples": total_samples,
            "estimated_memory_kb": estimated_memory_kb,
            "retention_hours": self.retention_hours,
            "alert_thresholds": len(self.alert_thresholds),
            "alert_callbacks": len(self.alert_callbacks),
            "metrics_list": list(self.metrics.keys())
        }


# Global metrics collector instance
metrics_collector = MetricsCollector()