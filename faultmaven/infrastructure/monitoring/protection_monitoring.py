# File: faultmaven/infrastructure/monitoring/protection_monitoring.py

import asyncio
import logging
import json
from datetime import datetime, timezone, timedelta
from faultmaven.utils.serialization import to_json_compatible
from typing import Dict, List, Any, Optional
from collections import defaultdict, deque
from dataclasses import dataclass
import statistics

from faultmaven.models.behavioral import RiskLevel, ReputationLevel


@dataclass
class AlertThreshold:
    """Alert threshold configuration"""
    metric: str
    threshold: float
    comparison: str  # "gt", "lt", "eq"
    severity: str  # "low", "medium", "high", "critical"
    enabled: bool = True


@dataclass
class AlertEvent:
    """Alert event data"""
    alert_id: str
    metric: str
    current_value: float
    threshold: float
    severity: str
    timestamp: datetime
    description: str
    metadata: Dict[str, Any]
    resolved: bool = False


class MetricsCollector:
    """Collects and aggregates intelligent protection metrics"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # Time-series storage (in-memory for this implementation)
        self.metrics_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self.metric_windows = {
            "1m": timedelta(minutes=1),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "24h": timedelta(hours=24)
        }
        
        # Metric definitions
        self.metric_definitions = {
            "protection.requests.total": "Total requests processed",
            "protection.requests.blocked": "Requests blocked by protection",
            "protection.requests.allowed": "Requests allowed through protection",
            "protection.anomalies.detected": "Anomalies detected per minute",
            "protection.reputation.updates": "Reputation updates per minute",
            "protection.circuit_breakers.opened": "Circuit breakers opened",
            "protection.circuit_breakers.closed": "Circuit breakers closed",
            "protection.false_positives": "False positive detections",
            "protection.true_positives": "True positive detections",
            "protection.processing_time.avg": "Average processing time (ms)",
            "protection.processing_time.p95": "95th percentile processing time (ms)",
            "protection.risk_distribution": "Distribution of risk levels",
            "protection.reputation_distribution": "Distribution of reputation levels",
            "protection.ml_model.accuracy": "ML model accuracy percentage",
            "protection.behavioral.profiles": "Active behavioral profiles",
            "protection.system.load": "System load factor"
        }
        
    def record_metric(self, metric: str, value: float, timestamp: Optional[datetime] = None, 
                     metadata: Optional[Dict[str, Any]] = None):
        """Record a metric value"""
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        metric_point = {
            "value": value,
            "timestamp": timestamp,
            "metadata": metadata or {}
        }
        
        self.metrics_data[metric].append(metric_point)
    
    def get_metric_value(self, metric: str, window: str = "1m") -> Optional[float]:
        """Get latest metric value within window"""
        if metric not in self.metrics_data:
            return None
        
        window_duration = self.metric_windows.get(window)
        if not window_duration:
            return None
        
        cutoff_time = datetime.now(timezone.utc) - window_duration
        recent_points = [
            point for point in self.metrics_data[metric]
            if point["timestamp"] > cutoff_time
        ]
        
        if not recent_points:
            return None
        
        # Return latest value
        return recent_points[-1]["value"]
    
    def get_metric_aggregation(self, metric: str, window: str = "1m", 
                              aggregation: str = "avg") -> Optional[float]:
        """Get aggregated metric value within window"""
        if metric not in self.metrics_data:
            return None
        
        window_duration = self.metric_windows.get(window)
        if not window_duration:
            return None
        
        cutoff_time = datetime.now(timezone.utc) - window_duration
        recent_points = [
            point["value"] for point in self.metrics_data[metric]
            if point["timestamp"] > cutoff_time
        ]
        
        if not recent_points:
            return None
        
        if aggregation == "avg":
            return statistics.mean(recent_points)
        elif aggregation == "sum":
            return sum(recent_points)
        elif aggregation == "max":
            return max(recent_points)
        elif aggregation == "min":
            return min(recent_points)
        elif aggregation == "count":
            return len(recent_points)
        elif aggregation == "p95":
            return statistics.quantiles(recent_points, n=20)[18] if len(recent_points) > 1 else recent_points[0]
        
        return None
    
    def get_all_metrics(self, window: str = "1m") -> Dict[str, float]:
        """Get all current metric values"""
        metrics = {}
        for metric in self.metric_definitions.keys():
            value = self.get_metric_value(metric, window)
            if value is not None:
                metrics[metric] = value
        return metrics


class AlertManager:
    """Manages alerts for intelligent protection system"""
    
    def __init__(self, metrics_collector: MetricsCollector):
        self.metrics_collector = metrics_collector
        self.logger = logging.getLogger(__name__)
        
        # Alert configuration
        self.alert_thresholds = self._create_default_thresholds()
        self.active_alerts: Dict[str, AlertEvent] = {}
        self.alert_history: deque = deque(maxlen=1000)
        
        # Alert suppression (prevent spam)
        self.alert_cooldowns: Dict[str, datetime] = {}
        self.cooldown_duration = timedelta(minutes=5)
        
    def _create_default_thresholds(self) -> List[AlertThreshold]:
        """Create default alert thresholds"""
        return [
            AlertThreshold("protection.anomalies.detected", 10, "gt", "medium"),
            AlertThreshold("protection.requests.blocked", 50, "gt", "high"),
            AlertThreshold("protection.false_positives", 20, "gt", "medium"),
            AlertThreshold("protection.circuit_breakers.opened", 3, "gt", "high"),
            AlertThreshold("protection.processing_time.avg", 50, "gt", "medium"),
            AlertThreshold("protection.processing_time.p95", 100, "gt", "high"),
            AlertThreshold("protection.ml_model.accuracy", 70, "lt", "critical"),
            AlertThreshold("protection.system.load", 0.9, "gt", "critical"),
        ]
    
    async def check_alerts(self) -> List[AlertEvent]:
        """Check for alert conditions and generate alerts"""
        new_alerts = []
        
        for threshold in self.alert_thresholds:
            if not threshold.enabled:
                continue
            
            try:
                current_value = self.metrics_collector.get_metric_value(threshold.metric, "1m")
                if current_value is None:
                    continue
                
                # Check if alert condition is met
                alert_triggered = False
                if threshold.comparison == "gt" and current_value > threshold.threshold:
                    alert_triggered = True
                elif threshold.comparison == "lt" and current_value < threshold.threshold:
                    alert_triggered = True
                elif threshold.comparison == "eq" and abs(current_value - threshold.threshold) < 0.01:
                    alert_triggered = True
                
                if alert_triggered:
                    # Check cooldown
                    if self._is_in_cooldown(threshold.metric):
                        continue
                    
                    # Create alert
                    alert = AlertEvent(
                        alert_id=f"{threshold.metric}_{int(datetime.now(timezone.utc).timestamp())}",
                        metric=threshold.metric,
                        current_value=current_value,
                        threshold=threshold.threshold,
                        severity=threshold.severity,
                        timestamp=datetime.now(timezone.utc),
                        description=f"{threshold.metric} is {current_value} (threshold: {threshold.threshold})",
                        metadata={
                            "comparison": threshold.comparison,
                            "window": "1m"
                        }
                    )
                    
                    new_alerts.append(alert)
                    self.active_alerts[alert.alert_id] = alert
                    self.alert_history.append(alert)
                    
                    # Set cooldown
                    self.alert_cooldowns[threshold.metric] = datetime.now(timezone.utc)
                    
                    self.logger.warning(f"Alert triggered: {alert.description}")
                
                else:
                    # Check if we need to resolve any active alerts for this metric
                    await self._resolve_alerts_for_metric(threshold.metric)
            
            except Exception as e:
                self.logger.error(f"Error checking alert for {threshold.metric}: {e}")
        
        return new_alerts
    
    def _is_in_cooldown(self, metric: str) -> bool:
        """Check if metric is in alert cooldown period"""
        if metric not in self.alert_cooldowns:
            return False
        
        cooldown_end = self.alert_cooldowns[metric] + self.cooldown_duration
        return datetime.now(timezone.utc) < cooldown_end
    
    async def _resolve_alerts_for_metric(self, metric: str):
        """Resolve active alerts for a metric when conditions are normal"""
        for alert_id, alert in list(self.active_alerts.items()):
            if alert.metric == metric and not alert.resolved:
                alert.resolved = True
                self.logger.info(f"Alert resolved: {alert.description}")
    
    def get_active_alerts(self) -> List[AlertEvent]:
        """Get currently active alerts"""
        return [alert for alert in self.active_alerts.values() if not alert.resolved]
    
    def get_alert_summary(self) -> Dict[str, Any]:
        """Get alert summary statistics"""
        active_alerts = self.get_active_alerts()
        
        severity_counts = defaultdict(int)
        for alert in active_alerts:
            severity_counts[alert.severity] += 1
        
        return {
            "total_active": len(active_alerts),
            "severity_breakdown": dict(severity_counts),
            "recent_alerts": len([a for a in self.alert_history if 
                                (datetime.now(timezone.utc) - a.timestamp) < timedelta(hours=1)]),
            "oldest_active": min([a.timestamp for a in active_alerts]) if active_alerts else None
        }


class ProtectionMonitor:
    """Comprehensive monitoring system for intelligent protection"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.metrics_collector = MetricsCollector()
        self.alert_manager = AlertManager(self.metrics_collector)
        
        # Background tasks
        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Performance tracking
        self.monitoring_interval = 60  # seconds
        self.last_monitoring_run = None
        self.monitoring_errors = 0
        
    async def start_monitoring(self):
        """Start background monitoring"""
        if self._monitoring_task and not self._monitoring_task.done():
            self.logger.warning("Monitoring already running")
            return
        
        self.logger.info("Starting intelligent protection monitoring")
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
    
    async def stop_monitoring(self):
        """Stop background monitoring"""
        if not self._monitoring_task or self._monitoring_task.done():
            return
        
        self.logger.info("Stopping intelligent protection monitoring")
        self._shutdown_event.set()
        
        try:
            await asyncio.wait_for(self._monitoring_task, timeout=10.0)
        except asyncio.TimeoutError:
            self.logger.warning("Monitoring stop timed out, cancelling task")
            self._monitoring_task.cancel()
    
    async def _monitoring_loop(self):
        """Main monitoring loop"""
        while not self._shutdown_event.is_set():
            try:
                start_time = datetime.now(timezone.utc)
                
                # Check alerts
                new_alerts = await self.alert_manager.check_alerts()
                
                # Log new alerts
                for alert in new_alerts:
                    await self._handle_new_alert(alert)
                
                # Update monitoring metrics
                self.last_monitoring_run = start_time
                processing_time = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
                self.metrics_collector.record_metric("monitoring.processing_time", processing_time)
                
                # Wait for next interval
                await asyncio.sleep(self.monitoring_interval)
                
            except Exception as e:
                self.monitoring_errors += 1
                self.logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(min(self.monitoring_interval, 60))  # Back off on errors
    
    async def _handle_new_alert(self, alert: AlertEvent):
        """Handle a new alert"""
        try:
            # Log based on severity
            if alert.severity == "critical":
                self.logger.critical(f"CRITICAL ALERT: {alert.description}")
            elif alert.severity == "high":
                self.logger.error(f"HIGH ALERT: {alert.description}")
            elif alert.severity == "medium":
                self.logger.warning(f"MEDIUM ALERT: {alert.description}")
            else:
                self.logger.info(f"LOW ALERT: {alert.description}")
            
            # Could integrate with external alerting systems here
            # - Email notifications
            # - Slack/Teams messages  
            # - PagerDuty incidents
            # - SNMP traps
            
        except Exception as e:
            self.logger.error(f"Error handling alert: {e}")
    
    async def record_protection_event(self, event_type: str, metadata: Dict[str, Any]):
        """Record a protection-related event"""
        timestamp = datetime.now(timezone.utc)
        
        # Record basic metrics
        if event_type == "request_analyzed":
            self.metrics_collector.record_metric("protection.requests.total", 1, timestamp)
        elif event_type == "request_blocked":
            self.metrics_collector.record_metric("protection.requests.blocked", 1, timestamp)
        elif event_type == "request_allowed":
            self.metrics_collector.record_metric("protection.requests.allowed", 1, timestamp)
        elif event_type == "anomaly_detected":
            self.metrics_collector.record_metric("protection.anomalies.detected", 1, timestamp)
        elif event_type == "reputation_updated":
            self.metrics_collector.record_metric("protection.reputation.updates", 1, timestamp)
        elif event_type == "circuit_breaker_opened":
            self.metrics_collector.record_metric("protection.circuit_breakers.opened", 1, timestamp)
        elif event_type == "circuit_breaker_closed":
            self.metrics_collector.record_metric("protection.circuit_breakers.closed", 1, timestamp)
        elif event_type == "false_positive":
            self.metrics_collector.record_metric("protection.false_positives", 1, timestamp)
        elif event_type == "true_positive":
            self.metrics_collector.record_metric("protection.true_positives", 1, timestamp)
        
        # Record processing time if available
        if "processing_time" in metadata:
            self.metrics_collector.record_metric("protection.processing_time.avg", 
                                               metadata["processing_time"], timestamp)
        
        # Record risk level distribution
        if "risk_level" in metadata:
            risk_level = metadata["risk_level"]
            if isinstance(risk_level, RiskLevel):
                risk_level = risk_level.value
            self.metrics_collector.record_metric(f"protection.risk_distribution.{risk_level}", 
                                               1, timestamp)
        
        # Record reputation level distribution
        if "reputation_level" in metadata:
            rep_level = metadata["reputation_level"]
            if isinstance(rep_level, ReputationLevel):
                rep_level = rep_level.value
            self.metrics_collector.record_metric(f"protection.reputation_distribution.{rep_level}", 
                                               1, timestamp)
    
    async def get_monitoring_dashboard(self) -> Dict[str, Any]:
        """Get comprehensive monitoring dashboard data"""
        try:
            dashboard = {
                "timestamp": to_json_compatible(datetime.now(timezone.utc)),
                "monitoring_status": {
                    "active": self._monitoring_task and not self._monitoring_task.done(),
                    "last_run": self.to_json_compatible(last_monitoring_run) if self.last_monitoring_run else None,
                    "errors": self.monitoring_errors
                },
                "alerts": {
                    "active": self.alert_manager.get_active_alerts(),
                    "summary": self.alert_manager.get_alert_summary()
                },
                "metrics": {
                    "current": self.metrics_collector.get_all_metrics("1m"),
                    "5min_avg": {},
                    "1hour_avg": {}
                },
                "trends": await self._calculate_trends(),
                "health_score": await self._calculate_health_score()
            }
            
            # Calculate averages for different windows
            for metric in self.metrics_collector.metric_definitions.keys():
                dashboard["metrics"]["5min_avg"][metric] = self.metrics_collector.get_metric_aggregation(metric, "5m", "avg")
                dashboard["metrics"]["1hour_avg"][metric] = self.metrics_collector.get_metric_aggregation(metric, "1h", "avg")
            
            return dashboard
            
        except Exception as e:
            self.logger.error(f"Error generating monitoring dashboard: {e}")
            return {"error": str(e), "timestamp": to_json_compatible(datetime.now(timezone.utc))}
    
    async def _calculate_trends(self) -> Dict[str, str]:
        """Calculate trend directions for key metrics"""
        trends = {}
        
        key_metrics = [
            "protection.requests.total",
            "protection.requests.blocked", 
            "protection.anomalies.detected",
            "protection.processing_time.avg"
        ]
        
        for metric in key_metrics:
            try:
                recent_5m = self.metrics_collector.get_metric_aggregation(metric, "5m", "avg")
                recent_1h = self.metrics_collector.get_metric_aggregation(metric, "1h", "avg")
                
                if recent_5m is not None and recent_1h is not None:
                    if recent_5m > recent_1h * 1.1:
                        trends[metric] = "increasing"
                    elif recent_5m < recent_1h * 0.9:
                        trends[metric] = "decreasing"
                    else:
                        trends[metric] = "stable"
                else:
                    trends[metric] = "unknown"
            except Exception:
                trends[metric] = "unknown"
        
        return trends
    
    async def _calculate_health_score(self) -> float:
        """Calculate overall protection system health score (0.0 to 1.0)"""
        try:
            score_factors = []
            
            # Factor 1: Alert severity
            active_alerts = self.alert_manager.get_active_alerts()
            if not active_alerts:
                alert_factor = 1.0
            else:
                critical_alerts = sum(1 for a in active_alerts if a.severity == "critical")
                high_alerts = sum(1 for a in active_alerts if a.severity == "high")
                
                alert_factor = max(0.0, 1.0 - (critical_alerts * 0.3 + high_alerts * 0.2))
            
            score_factors.append(alert_factor)
            
            # Factor 2: Processing performance
            avg_processing_time = self.metrics_collector.get_metric_value("protection.processing_time.avg", "5m")
            if avg_processing_time is not None:
                # Good performance < 10ms, poor performance > 50ms
                perf_factor = max(0.0, 1.0 - max(0, avg_processing_time - 10) / 40)
            else:
                perf_factor = 0.8  # Neutral if no data
            
            score_factors.append(perf_factor)
            
            # Factor 3: False positive rate
            false_positives = self.metrics_collector.get_metric_aggregation("protection.false_positives", "1h", "sum") or 0
            total_requests = self.metrics_collector.get_metric_aggregation("protection.requests.total", "1h", "sum") or 1
            
            false_positive_rate = false_positives / total_requests
            fp_factor = max(0.0, 1.0 - false_positive_rate * 5)  # Penalize high FP rates
            
            score_factors.append(fp_factor)
            
            # Factor 4: System stability (monitoring errors)
            if self.monitoring_errors > 10:
                stability_factor = 0.5
            elif self.monitoring_errors > 5:
                stability_factor = 0.7
            else:
                stability_factor = 1.0
            
            score_factors.append(stability_factor)
            
            # Calculate weighted average
            weights = [0.4, 0.3, 0.2, 0.1]  # Alert severity weighted most heavily
            health_score = sum(factor * weight for factor, weight in zip(score_factors, weights))
            
            return max(0.0, min(1.0, health_score))
            
        except Exception as e:
            self.logger.error(f"Error calculating health score: {e}")
            return 0.5  # Neutral score on error
    
    async def export_metrics(self, format: str = "prometheus") -> str:
        """Export metrics in specified format"""
        try:
            if format == "prometheus":
                return await self._export_prometheus_format()
            elif format == "json":
                return json.dumps(self.metrics_collector.get_all_metrics("1m"), indent=2)
            else:
                raise ValueError(f"Unsupported export format: {format}")
        
        except Exception as e:
            self.logger.error(f"Error exporting metrics: {e}")
            return ""
    
    async def _export_prometheus_format(self) -> str:
        """Export metrics in Prometheus format"""
        lines = []
        
        # Add metric definitions as comments
        for metric, description in self.metrics_collector.metric_definitions.items():
            lines.append(f"# HELP {metric.replace('.', '_')} {description}")
            lines.append(f"# TYPE {metric.replace('.', '_')} gauge")
            
            value = self.metrics_collector.get_metric_value(metric, "1m")
            if value is not None:
                lines.append(f"{metric.replace('.', '_')} {value}")
            
            lines.append("")  # Blank line between metrics
        
        return "\n".join(lines)


# Global monitor instance
protection_monitor = ProtectionMonitor()