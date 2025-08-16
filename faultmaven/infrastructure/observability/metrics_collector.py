"""Advanced Metrics Collection Service for FaultMaven Phase 2

This module provides comprehensive performance monitoring and metrics collection
for the intelligent troubleshooting system, including real-time analytics,
performance optimization insights, and proactive bottleneck detection.

Key Features:
- Real-time performance metrics collection
- Service-specific performance tracking
- User pattern analysis and optimization insights
- Cross-session learning effectiveness metrics
- Knowledge base growth and optimization metrics
- Multi-step workflow success analysis
- Automatic performance bottleneck detection
- Intelligent caching strategies based on usage patterns

Performance Targets:
- Metrics collection overhead: < 5ms
- Analytics query response: < 100ms  
- Dashboard updates: < 200ms
- Optimization recommendations: < 500ms
"""

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from contextlib import contextmanager
import threading
from concurrent.futures import ThreadPoolExecutor
import json
import statistics

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.models.interfaces import ITracer


@dataclass
class MetricData:
    """Individual metric data point"""
    timestamp: datetime
    service: str
    operation: str
    value: float
    unit: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: Dict[str, str] = field(default_factory=dict)


@dataclass
class PerformanceSnapshot:
    """Performance snapshot for a specific time window"""
    timestamp: datetime
    service: str
    metrics: Dict[str, Any]
    alerts: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class ServicePerformanceProfile:
    """Performance profile for a specific service"""
    service_name: str
    operation_metrics: Dict[str, Dict[str, float]]  # operation -> {avg, p95, p99, count}
    error_rates: Dict[str, float]  # operation -> error_rate
    resource_utilization: Dict[str, float]  # memory, cpu, etc.
    optimization_opportunities: List[str]
    last_updated: datetime


class MetricsCollector(BaseExternalClient):
    """Advanced metrics collection service for FaultMaven Phase 2
    
    This service provides comprehensive performance monitoring and analytics
    for all Phase 2 intelligent troubleshooting components including:
    - Memory service performance tracking
    - Planning service metrics collection
    - Knowledge service performance analysis
    - Orchestration workflow metrics
    - Agent service performance monitoring
    - Cross-component performance correlation
    - Proactive optimization recommendations
    """
    
    def __init__(
        self,
        tracer: Optional[ITracer] = None,
        buffer_size: int = 10000,
        flush_interval: int = 60,
        analytics_window: int = 300  # 5 minutes
    ):
        """Initialize the metrics collector
        
        Args:
            tracer: Optional tracer for observability integration
            buffer_size: Maximum number of metrics to buffer
            flush_interval: Interval in seconds to flush metrics
            analytics_window: Time window in seconds for analytics calculations
        """
        super().__init__(
            client_name="MetricsCollector",
            service_name="FaultMaven-Metrics",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30
        )
        
        self._tracer = tracer
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval
        self._analytics_window = analytics_window
        
        # Thread-safe metric storage
        self._metrics_buffer: deque = deque(maxlen=buffer_size)
        self._buffer_lock = threading.RLock()
        
        # Service performance profiles
        self._service_profiles: Dict[str, ServicePerformanceProfile] = {}
        self._profile_lock = threading.RLock()
        
        # Real-time analytics data
        self._analytics_data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._analytics_lock = threading.RLock()
        
        # Performance thresholds and SLAs
        self._performance_thresholds = {
            "memory_service": {
                "context_retrieval": {"target": 50, "warning": 75, "critical": 100},  # ms
                "consolidation": {"target": 200, "warning": 500, "critical": 1000},
                "profile_operations": {"target": 100, "warning": 150, "critical": 200}
            },
            "planning_service": {
                "strategy_generation": {"target": 300, "warning": 500, "critical": 800},
                "plan_optimization": {"target": 200, "warning": 400, "critical": 600}
            },
            "knowledge_service": {
                "search_time": {"target": 100, "warning": 200, "critical": 500},
                "relevance_scoring": {"target": 50, "warning": 100, "critical": 200}
            },
            "orchestration_service": {
                "workflow_creation": {"target": 500, "warning": 1000, "critical": 2000},
                "step_execution": {"target": 3000, "warning": 5000, "critical": 10000},
                "status_retrieval": {"target": 100, "warning": 200, "critical": 500}
            },
            "agent_service": {
                "query_processing": {"target": 2000, "warning": 4000, "critical": 8000},
                "confidence_calculation": {"target": 100, "warning": 200, "critical": 400}
            }
        }
        
        # Performance optimization recommendations
        self._optimization_rules = []
        self._setup_optimization_rules()
        
        # Caching statistics
        self._cache_stats = defaultdict(lambda: {
            "hits": 0, "misses": 0, "hit_rate": 0.0, "avg_retrieval_time": 0.0
        })
        
        # Background processing
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metrics")
        self._background_tasks_running = False
        
        # Performance alerts
        self._active_alerts: Set[str] = set()
        self._alert_history: deque = deque(maxlen=1000)
        
        self.logger.info("MetricsCollector initialized with advanced performance monitoring")
    
    async def start_background_processing(self):
        """Start background metric processing tasks"""
        if self._background_tasks_running:
            return
        
        self._background_tasks_running = True
        
        # Start background tasks
        asyncio.create_task(self._periodic_flush())
        asyncio.create_task(self._analytics_processor())
        asyncio.create_task(self._performance_monitor())
        
        self.logger.info("Background metric processing tasks started")
    
    async def stop_background_processing(self):
        """Stop background metric processing tasks"""
        self._background_tasks_running = False
        self._executor.shutdown(wait=True)
        self.logger.info("Background metric processing tasks stopped")
    
    def record_metric(
        self,
        service: str,
        operation: str,
        value: float,
        unit: str = "milliseconds",
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None
    ) -> None:
        """Record a performance metric
        
        Args:
            service: Name of the service (e.g., "memory_service", "planning_service")
            operation: Operation being measured (e.g., "context_retrieval")
            value: Metric value (typically timing in milliseconds)
            unit: Unit of measurement
            metadata: Additional metadata
            tags: Tags for metric categorization
        """
        try:
            metric = MetricData(
                timestamp=datetime.utcnow(),
                service=service,
                operation=operation,
                value=value,
                unit=unit,
                metadata=metadata or {},
                tags=tags or {}
            )
            
            with self._buffer_lock:
                self._metrics_buffer.append(metric)
            
            # Update real-time analytics
            self._update_real_time_analytics(metric)
            
            # Check for performance threshold violations
            self._check_performance_thresholds(metric)
            
        except Exception as e:
            self.logger.error(f"Failed to record metric: {e}")
    
    @contextmanager
    def measure_operation(
        self,
        service: str,
        operation: str,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None
    ):
        """Context manager for measuring operation duration
        
        Args:
            service: Service name
            operation: Operation name
            metadata: Additional metadata
            tags: Metric tags
        """
        start_time = time.time()
        error_occurred = False
        
        try:
            yield
        except Exception as e:
            error_occurred = True
            # Record error metric
            self.record_metric(
                service=service,
                operation=f"{operation}_error",
                value=1,
                unit="count",
                metadata={**(metadata or {}), "error": str(e)},
                tags=tags
            )
            raise
        finally:
            duration_ms = (time.time() - start_time) * 1000
            
            # Record timing metric
            self.record_metric(
                service=service,
                operation=operation,
                value=duration_ms,
                unit="milliseconds",
                metadata={
                    **(metadata or {}),
                    "success": not error_occurred
                },
                tags=tags
            )
    
    def record_cache_event(
        self,
        service: str,
        cache_key: str,
        event_type: str,  # "hit", "miss", "set", "evict"
        retrieval_time_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record cache performance event
        
        Args:
            service: Service name
            cache_key: Cache key (will be hashed for privacy)
            event_type: Type of cache event
            retrieval_time_ms: Time taken for retrieval
            metadata: Additional metadata
        """
        try:
            # Hash cache key for privacy
            import hashlib
            hashed_key = hashlib.sha256(cache_key.encode()).hexdigest()[:16]
            
            cache_metadata = {
                "cache_key_hash": hashed_key,
                "event_type": event_type,
                **(metadata or {})
            }
            
            if retrieval_time_ms is not None:
                cache_metadata["retrieval_time_ms"] = retrieval_time_ms
            
            self.record_metric(
                service=service,
                operation="cache_operation",
                value=1,
                unit="count",
                metadata=cache_metadata,
                tags={"event_type": event_type}
            )
            
            # Update cache statistics
            with self._analytics_lock:
                stats = self._cache_stats[service]
                if event_type == "hit":
                    stats["hits"] += 1
                elif event_type == "miss":
                    stats["misses"] += 1
                
                total_requests = stats["hits"] + stats["misses"]
                if total_requests > 0:
                    stats["hit_rate"] = stats["hits"] / total_requests
                
                if retrieval_time_ms is not None:
                    # Update running average
                    current_avg = stats["avg_retrieval_time"]
                    stats["avg_retrieval_time"] = (
                        (current_avg * (total_requests - 1) + retrieval_time_ms) / total_requests
                        if total_requests > 1 else retrieval_time_ms
                    )
            
        except Exception as e:
            self.logger.error(f"Failed to record cache event: {e}")
    
    def record_user_pattern(
        self,
        session_id: str,
        user_id: str,
        pattern_type: str,  # "query", "workflow", "preference"
        pattern_data: Dict[str, Any],
        effectiveness_score: Optional[float] = None
    ) -> None:
        """Record user interaction pattern for analysis
        
        Args:
            session_id: Session identifier
            user_id: User identifier (will be hashed for privacy)
            pattern_type: Type of pattern being recorded
            pattern_data: Pattern-specific data
            effectiveness_score: Optional effectiveness score (0-1)
        """
        try:
            import hashlib
            hashed_user_id = hashlib.sha256(user_id.encode()).hexdigest()[:16]
            
            pattern_metadata = {
                "session_id": session_id,
                "user_id_hash": hashed_user_id,
                "pattern_type": pattern_type,
                "pattern_data": pattern_data
            }
            
            if effectiveness_score is not None:
                pattern_metadata["effectiveness_score"] = effectiveness_score
            
            self.record_metric(
                service="user_analytics",
                operation="pattern_analysis",
                value=effectiveness_score or 1.0,
                unit="score",
                metadata=pattern_metadata,
                tags={"pattern_type": pattern_type}
            )
            
        except Exception as e:
            self.logger.error(f"Failed to record user pattern: {e}")
    
    def record_workflow_metrics(
        self,
        workflow_id: str,
        phase: str,
        step_number: int,
        execution_time_ms: float,
        success: bool,
        findings_count: int,
        knowledge_items_retrieved: int,
        confidence_score: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record comprehensive workflow execution metrics
        
        Args:
            workflow_id: Workflow identifier
            phase: Current troubleshooting phase
            step_number: Step number in workflow
            execution_time_ms: Step execution time
            success: Whether step completed successfully
            findings_count: Number of findings discovered
            knowledge_items_retrieved: Number of knowledge items retrieved
            confidence_score: Optional confidence score for step outcome
            metadata: Additional metadata
        """
        try:
            workflow_metadata = {
                "workflow_id": workflow_id,
                "phase": phase,
                "step_number": step_number,
                "success": success,
                "findings_count": findings_count,
                "knowledge_items_retrieved": knowledge_items_retrieved,
                **(metadata or {})
            }
            
            if confidence_score is not None:
                workflow_metadata["confidence_score"] = confidence_score
            
            # Record main workflow timing
            self.record_metric(
                service="orchestration_service",
                operation="workflow_step_execution",
                value=execution_time_ms,
                unit="milliseconds",
                metadata=workflow_metadata,
                tags={
                    "phase": phase,
                    "success": str(success),
                    "step": str(step_number)
                }
            )
            
            # Record workflow effectiveness metrics
            if findings_count > 0:
                self.record_metric(
                    service="orchestration_service",
                    operation="workflow_effectiveness",
                    value=findings_count,
                    unit="count",
                    metadata=workflow_metadata,
                    tags={"metric_type": "findings"}
                )
            
            if knowledge_items_retrieved > 0:
                self.record_metric(
                    service="orchestration_service",
                    operation="workflow_effectiveness", 
                    value=knowledge_items_retrieved,
                    unit="count",
                    metadata=workflow_metadata,
                    tags={"metric_type": "knowledge_retrieval"}
                )
            
        except Exception as e:
            self.logger.error(f"Failed to record workflow metrics: {e}")
    
    async def get_service_performance_summary(
        self,
        service: str,
        time_window_minutes: int = 60
    ) -> Dict[str, Any]:
        """Get performance summary for a specific service
        
        Args:
            service: Service name
            time_window_minutes: Time window for analysis
            
        Returns:
            Performance summary with metrics, trends, and recommendations
        """
        try:
            cutoff_time = datetime.utcnow() - timedelta(minutes=time_window_minutes)
            
            # Filter metrics for service and time window
            relevant_metrics = []
            with self._buffer_lock:
                relevant_metrics = [
                    m for m in self._metrics_buffer
                    if m.service == service and m.timestamp >= cutoff_time
                ]
            
            if not relevant_metrics:
                return {
                    "service": service,
                    "time_window_minutes": time_window_minutes,
                    "status": "no_data",
                    "message": "No metrics available for the specified time window"
                }
            
            # Group metrics by operation
            operations = defaultdict(list)
            for metric in relevant_metrics:
                operations[metric.operation].append(metric.value)
            
            # Calculate statistics for each operation
            operation_stats = {}
            alerts = []
            recommendations = []
            
            for operation, values in operations.items():
                if not values:
                    continue
                
                stats = {
                    "count": len(values),
                    "avg": statistics.mean(values),
                    "min": min(values),
                    "max": max(values),
                    "median": statistics.median(values)
                }
                
                if len(values) > 1:
                    stats["std_dev"] = statistics.stdev(values)
                    
                    # Calculate percentiles
                    sorted_values = sorted(values)
                    stats["p95"] = sorted_values[int(0.95 * len(sorted_values))]
                    stats["p99"] = sorted_values[int(0.99 * len(sorted_values))]
                
                operation_stats[operation] = stats
                
                # Check against thresholds
                thresholds = self._performance_thresholds.get(service, {}).get(operation)
                if thresholds and stats["avg"] > thresholds["warning"]:
                    severity = "critical" if stats["avg"] > thresholds["critical"] else "warning"
                    alerts.append({
                        "severity": severity,
                        "operation": operation,
                        "message": f"{operation} average response time ({stats['avg']:.1f}ms) exceeds {severity} threshold",
                        "threshold": thresholds[severity],
                        "actual_value": stats["avg"]
                    })
                
                # Generate recommendations
                if stats["avg"] > thresholds.get("target", 0) * 1.5:
                    recommendations.extend(self._generate_operation_recommendations(service, operation, stats))
            
            # Calculate overall health score
            health_score = self._calculate_service_health_score(service, operation_stats)
            
            # Get cache performance if available
            cache_stats = self._cache_stats.get(service, {})
            
            performance_summary = {
                "service": service,
                "time_window_minutes": time_window_minutes,
                "status": "healthy" if health_score > 0.8 else "degraded" if health_score > 0.6 else "critical",
                "health_score": health_score,
                "total_operations": sum(stats["count"] for stats in operation_stats.values()),
                "operation_statistics": operation_stats,
                "alerts": alerts,
                "recommendations": recommendations,
                "cache_performance": cache_stats,
                "analysis_timestamp": datetime.utcnow().isoformat()
            }
            
            # Update service profile
            await self._update_service_profile(service, performance_summary)
            
            return performance_summary
            
        except Exception as e:
            self.logger.error(f"Failed to get service performance summary: {e}")
            return {
                "service": service,
                "status": "error",
                "error": str(e)
            }
    
    async def get_system_performance_dashboard(self) -> Dict[str, Any]:
        """Get comprehensive system performance dashboard
        
        Returns:
            Complete system performance dashboard with all services and metrics
        """
        try:
            dashboard_data = {
                "timestamp": datetime.utcnow().isoformat(),
                "system_status": "healthy",
                "services": {},
                "cross_service_metrics": {},
                "performance_trends": {},
                "optimization_recommendations": [],
                "alerts": {
                    "active": list(self._active_alerts),
                    "recent": list(self._alert_history)[-10:]  # Last 10 alerts
                }
            }
            
            # Get performance summary for all services
            services = [
                "memory_service", "planning_service", "knowledge_service",
                "orchestration_service", "agent_service", "enhanced_agent_service"
            ]
            
            overall_health_scores = []
            
            for service in services:
                try:
                    service_summary = await self.get_service_performance_summary(service, 60)
                    dashboard_data["services"][service] = service_summary
                    
                    if "health_score" in service_summary:
                        overall_health_scores.append(service_summary["health_score"])
                    
                    # Aggregate alerts
                    if "alerts" in service_summary:
                        for alert in service_summary["alerts"]:
                            if alert["severity"] == "critical":
                                dashboard_data["system_status"] = "critical"
                            elif alert["severity"] == "warning" and dashboard_data["system_status"] == "healthy":
                                dashboard_data["system_status"] = "degraded"
                    
                except Exception as e:
                    self.logger.error(f"Failed to get summary for service {service}: {e}")
                    dashboard_data["services"][service] = {"status": "error", "error": str(e)}
            
            # Calculate overall system health
            if overall_health_scores:
                system_health_score = statistics.mean(overall_health_scores)
                dashboard_data["system_health_score"] = system_health_score
                
                if system_health_score < 0.6:
                    dashboard_data["system_status"] = "critical"
                elif system_health_score < 0.8:
                    dashboard_data["system_status"] = "degraded"
            
            # Add cross-service correlation metrics
            dashboard_data["cross_service_metrics"] = await self._analyze_cross_service_performance()
            
            # Add performance trends
            dashboard_data["performance_trends"] = await self._analyze_performance_trends()
            
            # Generate system-wide optimization recommendations
            dashboard_data["optimization_recommendations"] = await self._generate_system_recommendations()
            
            return dashboard_data
            
        except Exception as e:
            self.logger.error(f"Failed to generate performance dashboard: {e}")
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "status": "error",
                "error": str(e)
            }
    
    async def get_optimization_recommendations(
        self,
        service: Optional[str] = None,
        priority: str = "high"
    ) -> Dict[str, Any]:
        """Get performance optimization recommendations
        
        Args:
            service: Optional service to focus on
            priority: Priority level for recommendations
            
        Returns:
            Optimization recommendations with implementation guidance
        """
        try:
            recommendations = {
                "timestamp": datetime.utcnow().isoformat(),
                "priority": priority,
                "service_filter": service,
                "recommendations": []
            }
            
            # Analyze current performance profiles
            if service:
                services_to_analyze = [service]
            else:
                services_to_analyze = list(self._service_profiles.keys())
            
            for svc in services_to_analyze:
                if svc in self._service_profiles:
                    profile = self._service_profiles[svc]
                    service_recommendations = await self._analyze_service_for_optimizations(profile)
                    recommendations["recommendations"].extend(service_recommendations)
            
            # Sort by priority and impact
            recommendations["recommendations"].sort(
                key=lambda x: (x.get("priority_score", 0), x.get("impact_score", 0)),
                reverse=True
            )
            
            # Limit based on priority
            if priority == "high":
                recommendations["recommendations"] = recommendations["recommendations"][:5]
            elif priority == "medium":
                recommendations["recommendations"] = recommendations["recommendations"][:10]
            
            return recommendations
            
        except Exception as e:
            self.logger.error(f"Failed to get optimization recommendations: {e}")
            return {
                "timestamp": datetime.utcnow().isoformat(),
                "error": str(e),
                "recommendations": []
            }
    
    def _update_real_time_analytics(self, metric: MetricData) -> None:
        """Update real-time analytics with new metric"""
        try:
            with self._analytics_lock:
                key = f"{metric.service}.{metric.operation}"
                self._analytics_data[key].append({
                    "timestamp": metric.timestamp,
                    "value": metric.value,
                    "metadata": metric.metadata
                })
                
                # Update service-level aggregates
                service_key = f"{metric.service}.aggregate"
                self._analytics_data[service_key].append({
                    "timestamp": metric.timestamp,
                    "operation": metric.operation,
                    "value": metric.value
                })
                
        except Exception as e:
            self.logger.error(f"Failed to update real-time analytics: {e}")
    
    def _check_performance_thresholds(self, metric: MetricData) -> None:
        """Check metric against performance thresholds and generate alerts"""
        try:
            thresholds = self._performance_thresholds.get(metric.service, {}).get(metric.operation)
            if not thresholds:
                return
            
            alert_key = f"{metric.service}.{metric.operation}"
            
            if metric.value > thresholds.get("critical", float('inf')):
                alert = f"CRITICAL: {metric.service} {metric.operation} ({metric.value:.1f}ms) exceeds critical threshold"
                self._active_alerts.add(alert_key)
                self._alert_history.append({
                    "timestamp": metric.timestamp.isoformat(),
                    "severity": "critical",
                    "service": metric.service,
                    "operation": metric.operation,
                    "value": metric.value,
                    "threshold": thresholds["critical"],
                    "message": alert
                })
                
            elif metric.value > thresholds.get("warning", float('inf')):
                alert = f"WARNING: {metric.service} {metric.operation} ({metric.value:.1f}ms) exceeds warning threshold"
                if alert_key not in self._active_alerts:  # Don't downgrade existing critical alerts
                    self._alert_history.append({
                        "timestamp": metric.timestamp.isoformat(),
                        "severity": "warning", 
                        "service": metric.service,
                        "operation": metric.operation,
                        "value": metric.value,
                        "threshold": thresholds["warning"],
                        "message": alert
                    })
            else:
                # Clear alert if performance is back to normal
                self._active_alerts.discard(alert_key)
                
        except Exception as e:
            self.logger.error(f"Failed to check performance thresholds: {e}")
    
    def _setup_optimization_rules(self) -> None:
        """Setup performance optimization rules"""
        self._optimization_rules = [
            {
                "name": "high_latency_operations",
                "condition": lambda stats: stats["avg"] > 1000,
                "recommendation": "Consider implementing caching or optimizing data access patterns",
                "priority": 0.8
            },
            {
                "name": "high_variability",
                "condition": lambda stats: stats.get("std_dev", 0) > stats["avg"] * 0.5,
                "recommendation": "High performance variability detected - consider connection pooling or resource optimization",
                "priority": 0.7
            },
            {
                "name": "frequent_operations",
                "condition": lambda stats: stats["count"] > 100,
                "recommendation": "Frequently used operation - consider implementing intelligent caching",
                "priority": 0.6
            }
        ]
    
    def _generate_operation_recommendations(
        self,
        service: str,
        operation: str,
        stats: Dict[str, Any]
    ) -> List[str]:
        """Generate recommendations for a specific operation"""
        recommendations = []
        
        for rule in self._optimization_rules:
            try:
                if rule["condition"](stats):
                    recommendations.append(f"{service}.{operation}: {rule['recommendation']}")
            except Exception as e:
                self.logger.error(f"Failed to evaluate optimization rule {rule['name']}: {e}")
        
        return recommendations
    
    def _calculate_service_health_score(
        self,
        service: str,
        operation_stats: Dict[str, Dict[str, Any]]
    ) -> float:
        """Calculate overall health score for a service (0.0 - 1.0)"""
        if not operation_stats:
            return 0.0
        
        scores = []
        thresholds = self._performance_thresholds.get(service, {})
        
        for operation, stats in operation_stats.items():
            threshold = thresholds.get(operation, {})
            if not threshold:
                scores.append(0.8)  # Default good score if no thresholds defined
                continue
            
            avg_time = stats["avg"]
            target = threshold.get("target", 0)
            critical = threshold.get("critical", float('inf'))
            
            if avg_time <= target:
                scores.append(1.0)  # Perfect performance
            elif avg_time >= critical:
                scores.append(0.0)  # Critical performance
            else:
                # Linear interpolation between target and critical
                score = 1.0 - (avg_time - target) / (critical - target)
                scores.append(max(0.0, score))
        
        return statistics.mean(scores) if scores else 0.0
    
    async def _update_service_profile(
        self,
        service: str,
        performance_summary: Dict[str, Any]
    ) -> None:
        """Update service performance profile"""
        try:
            with self._profile_lock:
                operation_metrics = {}
                error_rates = {}
                
                for operation, stats in performance_summary.get("operation_statistics", {}).items():
                    operation_metrics[operation] = {
                        "avg": stats["avg"],
                        "p95": stats.get("p95", stats["avg"]),
                        "p99": stats.get("p99", stats["avg"]),
                        "count": stats["count"]
                    }
                    
                    # Calculate error rate (simplified)
                    error_rates[operation] = 0.0  # Would be calculated from actual error metrics
                
                profile = ServicePerformanceProfile(
                    service_name=service,
                    operation_metrics=operation_metrics,
                    error_rates=error_rates,
                    resource_utilization={},  # Would be populated from system metrics
                    optimization_opportunities=performance_summary.get("recommendations", []),
                    last_updated=datetime.utcnow()
                )
                
                self._service_profiles[service] = profile
                
        except Exception as e:
            self.logger.error(f"Failed to update service profile for {service}: {e}")
    
    async def _analyze_cross_service_performance(self) -> Dict[str, Any]:
        """Analyze performance correlations across services"""
        # Placeholder for cross-service analysis
        return {
            "correlation_analysis": "Cross-service correlation analysis would be implemented here",
            "dependency_impact": {},
            "cascade_effects": []
        }
    
    async def _analyze_performance_trends(self) -> Dict[str, Any]:
        """Analyze performance trends over time"""
        # Placeholder for trend analysis
        return {
            "trending_up": [],
            "trending_down": [],
            "seasonal_patterns": {},
            "prediction_intervals": {}
        }
    
    async def _generate_system_recommendations(self) -> List[Dict[str, Any]]:
        """Generate system-wide optimization recommendations"""
        # Placeholder for system recommendations
        return [
            {
                "type": "system_optimization",
                "priority": "high",
                "title": "Implement intelligent caching layer",
                "description": "Add cross-service caching to reduce redundant operations",
                "estimated_impact": "15-25% performance improvement",
                "implementation_effort": "medium"
            }
        ]
    
    async def _analyze_service_for_optimizations(
        self,
        profile: ServicePerformanceProfile
    ) -> List[Dict[str, Any]]:
        """Analyze service profile for optimization opportunities"""
        recommendations = []
        
        for operation, metrics in profile.operation_metrics.items():
            if metrics["avg"] > 500:  # High latency operation
                recommendations.append({
                    "type": "performance_optimization",
                    "service": profile.service_name,
                    "operation": operation,
                    "priority": "high",
                    "title": f"Optimize {operation} performance",
                    "description": f"Average response time ({metrics['avg']:.1f}ms) exceeds optimal range",
                    "suggested_actions": [
                        "Implement operation-specific caching",
                        "Optimize data access patterns",
                        "Consider async processing where appropriate"
                    ],
                    "priority_score": 0.8,
                    "impact_score": 0.9
                })
        
        return recommendations
    
    async def _periodic_flush(self):
        """Periodically flush metrics buffer"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(self._flush_interval)
                
                # In a real implementation, this would flush to persistent storage
                with self._buffer_lock:
                    buffer_size = len(self._metrics_buffer)
                    if buffer_size > 0:
                        self.logger.debug(f"Flushing {buffer_size} metrics to storage")
                        # Metrics would be persisted here
                
            except Exception as e:
                self.logger.error(f"Error in periodic flush: {e}")
    
    async def _analytics_processor(self):
        """Process analytics data in background"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(30)  # Process every 30 seconds
                
                # Process real-time analytics
                with self._analytics_lock:
                    for key, data in self._analytics_data.items():
                        if len(data) > 10:  # Sufficient data for analysis
                            # Perform analytics calculations
                            pass
                
            except Exception as e:
                self.logger.error(f"Error in analytics processor: {e}")
    
    async def _performance_monitor(self):
        """Monitor system performance and generate alerts"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(60)  # Monitor every minute
                
                # Check for performance degradation patterns
                # Generate proactive alerts
                # Update optimization recommendations
                
            except Exception as e:
                self.logger.error(f"Error in performance monitor: {e}")
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of metrics collector"""
        base_health = await super().health_check()
        
        metrics_health = {
            **base_health,
            "service": "metrics_collector",
            "buffer_size": len(self._metrics_buffer),
            "max_buffer_size": self._buffer_size,
            "service_profiles": len(self._service_profiles),
            "active_alerts": len(self._active_alerts),
            "background_processing": self._background_tasks_running,
            "analytics_data_points": sum(len(data) for data in self._analytics_data.values()),
            "cache_stats": dict(self._cache_stats)
        }
        
        # Determine status
        if len(self._metrics_buffer) > self._buffer_size * 0.9:
            metrics_health["status"] = "degraded"
            metrics_health["warning"] = "Metrics buffer near capacity"
        elif not self._background_tasks_running:
            metrics_health["status"] = "degraded" 
            metrics_health["warning"] = "Background processing not running"
        
        return metrics_health