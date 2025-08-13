"""
Integration tests for Phase 2 monitoring enhancements.

These tests verify that the Phase 2 monitoring components work together
correctly and provide the expected functionality.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, AsyncMock

# Import Phase 2 components
from faultmaven.exceptions import ErrorSeverity, RecoveryResult
from faultmaven.infrastructure.error_recovery import (
    RecoveryManager, RetryRecoveryStrategy, CircuitBreakerRecoveryStrategy, 
    FallbackRecoveryStrategy
)
from faultmaven.infrastructure.health.component_monitor import (
    ComponentHealthMonitor, ComponentHealth, HealthStatus
)
from faultmaven.infrastructure.health.sla_tracker import (
    SLATracker, SLAMetrics, SLAStatus
)
from faultmaven.infrastructure.monitoring.metrics_collector import (
    MetricsCollector, PerformanceMetrics
)
from faultmaven.infrastructure.monitoring.apm_integration import (
    APMIntegration, APMMetrics, APMProvider
)
from faultmaven.infrastructure.monitoring.alerting import (
    AlertManager, AlertRule, Alert, AlertSeverity, AlertChannel
)


class TestPhase2ErrorRecovery:
    """Test error recovery framework functionality."""
    
    def test_recovery_manager_initialization(self):
        """Test that recovery manager initializes correctly."""
        manager = RecoveryManager()
        
        assert "retry" in manager.strategies
        assert "circuit_breaker" in manager.strategies
        assert "fallback" in manager.strategies
        assert len(manager.recovery_history) == 0
    
    @pytest.mark.asyncio
    async def test_retry_strategy(self):
        """Test retry recovery strategy."""
        strategy = RetryRecoveryStrategy(max_retries=2, initial_delay=0.01)
        
        # Test applicable error
        error = TimeoutError("Connection timeout")
        assert strategy.is_applicable(error, "api")
        
        # Test execution
        context = {"operation": "test_operation", "retry_count": 0}
        result = await strategy.execute(error, context)
        assert result == RecoveryResult.SUCCESS
    
    @pytest.mark.asyncio
    async def test_circuit_breaker_strategy(self):
        """Test circuit breaker recovery strategy."""
        strategy = CircuitBreakerRecoveryStrategy(failure_threshold=2, timeout=1)
        
        # Test applicable layer
        error = Exception("Service error")
        assert strategy.is_applicable(error, "service")
        assert not strategy.is_applicable(error, "api")
        
        # Test execution - first failure
        context = {"service": "test_service", "layer": "service"}
        result = await strategy.execute(error, context)
        assert result in [RecoveryResult.PARTIAL, RecoveryResult.FAILED]
    
    @pytest.mark.asyncio
    async def test_fallback_strategy(self):
        """Test fallback recovery strategy."""
        strategy = FallbackRecoveryStrategy()
        
        # Test applicable to most errors
        error = Exception("Generic error")
        assert strategy.is_applicable(error, "service")
        
        # Test execution
        context = {"service": "llm_provider", "operation": "inference"}
        result = await strategy.execute(error, context)
        assert result == RecoveryResult.SUCCESS
        assert "fallback_response" in context
    
    @pytest.mark.asyncio
    async def test_recovery_manager_execution(self):
        """Test end-to-end recovery manager execution."""
        manager = RecoveryManager()
        
        error = TimeoutError("Request timeout")
        context = {"layer": "api", "operation": "request"}
        
        result = await manager.execute_recovery("retry", error, context)
        assert result == RecoveryResult.SUCCESS
        
        # Check that history was recorded
        assert len(manager.recovery_history) == 1
        assert manager.recovery_history[0]["strategy"] == "retry"
        assert manager.recovery_history[0]["error_type"] == "TimeoutError"


class TestPhase2HealthMonitoring:
    """Test health monitoring framework."""
    
    def test_component_monitor_initialization(self):
        """Test component monitor initialization."""
        monitor = ComponentHealthMonitor()
        
        # Should have default components registered
        assert "database" in monitor.component_health
        assert "llm_provider" in monitor.component_health
        assert "knowledge_base" in monitor.component_health
        
        # Should have dependency mappings
        assert "database" in monitor.dependency_map
        assert len(monitor.sla_thresholds) > 0
    
    @pytest.mark.asyncio
    async def test_component_health_check(self):
        """Test individual component health check."""
        monitor = ComponentHealthMonitor()
        
        # Test database health check
        health = await monitor.check_component_health("database")
        assert isinstance(health, ComponentHealth)
        assert health.component_name == "database"
        assert health.status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UNHEALTHY]
        assert health.response_time_ms >= 0
    
    @pytest.mark.asyncio
    async def test_all_components_health_check(self):
        """Test checking all components health."""
        monitor = ComponentHealthMonitor()
        
        health_results = await monitor.check_all_components()
        assert len(health_results) > 0
        
        for component_name, health in health_results.items():
            assert isinstance(health, ComponentHealth)
            assert health.component_name == component_name
    
    def test_overall_health_status(self):
        """Test overall system health determination."""
        monitor = ComponentHealthMonitor()
        
        overall_status, summary = monitor.get_overall_health_status()
        assert overall_status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN]
        assert "reason" in summary
        assert "total_components" in summary
    
    def test_sla_tracker_initialization(self):
        """Test SLA tracker initialization."""
        tracker = SLATracker()
        
        # Should have default thresholds
        assert len(tracker.component_thresholds) > 0
        assert "api" in tracker.component_thresholds
        assert "llm_provider" in tracker.component_thresholds
    
    def test_sla_metrics_calculation(self):
        """Test SLA metrics calculation."""
        tracker = SLATracker()
        
        # Test API metrics calculation
        metrics = tracker.calculate_sla_metrics("api", time_window_hours=1)
        assert isinstance(metrics, SLAMetrics)
        assert metrics.component_name == "api"
        assert 0 <= metrics.availability_percentage <= 100
        assert metrics.response_time_p50 >= 0
        assert metrics.response_time_p95 >= 0
        assert metrics.response_time_p99 >= 0
    
    def test_sla_summary(self):
        """Test SLA summary generation."""
        tracker = SLATracker()
        
        summary = tracker.get_sla_summary()
        assert "overall_sla" in summary
        assert "components" in summary
        assert "active_breaches" in summary
        assert 0 <= summary["overall_sla"] <= 100


class TestPhase2PerformanceMonitoring:
    """Test performance monitoring framework."""
    
    def test_metrics_collector_initialization(self):
        """Test metrics collector initialization."""
        collector = MetricsCollector()
        
        assert len(collector.metrics) == 0
        assert len(collector.alert_thresholds) > 0
        assert "api.request_duration" in collector.alert_thresholds
    
    def test_performance_metric_recording(self):
        """Test recording performance metrics."""
        collector = MetricsCollector()
        
        # Record a metric
        collector.record_performance_metric(
            component="api",
            operation="request",
            duration_ms=150.0,
            success=True,
            metadata={"endpoint": "/test"},
            tags={"method": "GET"}
        )
        
        # Check that metric was recorded
        assert len(collector.metrics) > 0
        
        # Check specific metric stores
        assert "api.request" in collector.metrics
        assert len(collector.metrics["api.request"]) == 1
    
    def test_timer_functionality(self):
        """Test start/stop timer functionality."""
        collector = MetricsCollector()
        
        # Start timer
        timer_id = collector.start_timer("test_operation")
        assert timer_id in collector.start_times
        
        # Stop timer
        import time
        time.sleep(0.01)  # Small delay to ensure measurable duration
        duration = collector.stop_timer(
            timer_id, 
            component="test", 
            operation="test_op", 
            success=True
        )
        
        assert duration > 0
        assert timer_id not in collector.start_times
        assert "test.test_op" in collector.metrics
    
    def test_counter_and_gauge_metrics(self):
        """Test counter and gauge metric recording."""
        collector = MetricsCollector()
        
        # Record counter metric
        collector.record_counter_metric("requests.total", 1.0, {"endpoint": "test"})
        assert "requests.total" in collector.metrics
        
        # Record gauge metric
        collector.record_gauge_metric("memory.usage", 75.5, {"unit": "percentage"})
        assert "gauge.memory.usage" in collector.metrics
    
    def test_aggregated_metrics(self):
        """Test metrics aggregation."""
        collector = MetricsCollector()
        
        # Record several metrics
        for i in range(5):
            collector.record_performance_metric(
                component="api",
                operation="request",
                duration_ms=100.0 + i * 10,
                success=True
            )
        
        # Get aggregated metrics
        aggregated = collector.get_aggregated_metrics("api.request", time_window_minutes=60)
        assert aggregated is not None
        assert aggregated.count == 5
        assert aggregated.avg_value > 0
        assert aggregated.min_value <= aggregated.max_value
    
    def test_dashboard_data(self):
        """Test dashboard data generation."""
        collector = MetricsCollector()
        
        # Record some metrics
        collector.record_performance_metric("api", "request", 150.0, True)
        collector.record_performance_metric("llm", "request", 1200.0, True)
        
        dashboard_data = collector.get_dashboard_data(time_window_minutes=60)
        assert "timestamp" in dashboard_data
        assert "metrics" in dashboard_data
        assert "summary" in dashboard_data
    
    def test_apm_integration_initialization(self):
        """Test APM integration initialization."""
        apm = APMIntegration(service_name="test_service")
        
        assert apm.service_name == "test_service"
        assert APMProvider.OPIK in apm.configurations
        assert len(apm.export_callbacks) > 0
    
    def test_apm_operation_recording(self):
        """Test APM operation recording."""
        apm = APMIntegration()
        
        # Record operation
        apm.record_operation(
            operation_name="test_operation",
            duration_ms=250.0,
            status="success",
            tags={"component": "api"},
            attributes={"user_id": "test_user"}
        )
        
        assert len(apm.metrics_buffer) == 1
        metric = apm.metrics_buffer[0]
        assert metric.operation_name == "test_operation"
        assert metric.duration_ms == 250.0
        assert metric.status == "success"
    
    @pytest.mark.asyncio
    async def test_apm_metrics_flush(self):
        """Test APM metrics flushing."""
        apm = APMIntegration()
        
        # Record some metrics
        for i in range(3):
            apm.record_operation(f"operation_{i}", 100.0 + i * 50, "success")
        
        assert len(apm.metrics_buffer) == 3
        
        # Flush metrics
        await apm.flush_metrics()
        
        # Buffer should be cleared
        assert len(apm.metrics_buffer) == 0


class TestPhase2Alerting:
    """Test alerting framework."""
    
    def test_alert_manager_initialization(self):
        """Test alert manager initialization."""
        manager = AlertManager()
        
        assert len(manager.alert_rules) == 0
        assert len(manager.active_alerts) == 0
        assert len(manager.channel_configs) > 0
        assert AlertChannel.LOG in manager.channel_configs
    
    def test_alert_rule_management(self):
        """Test adding and managing alert rules."""
        manager = AlertManager()
        
        # Add alert rule
        rule = AlertRule(
            rule_id="test_rule",
            name="Test Rule",
            description="Test alert rule",
            metric_name="test_metric",
            condition="greater_than",
            threshold_value=100.0,
            severity=AlertSeverity.MEDIUM,
            channels=[AlertChannel.LOG]
        )
        
        manager.add_alert_rule(rule)
        assert "test_rule" in manager.alert_rules
        
        # Enable/disable rule
        assert manager.disable_rule("test_rule")
        assert not manager.alert_rules["test_rule"].enabled
        
        assert manager.enable_rule("test_rule")
        assert manager.alert_rules["test_rule"].enabled
        
        # Remove rule
        assert manager.remove_alert_rule("test_rule")
        assert "test_rule" not in manager.alert_rules
    
    def test_metric_evaluation(self):
        """Test metric evaluation against alert rules."""
        manager = AlertManager()
        
        # Add test rule
        rule = AlertRule(
            rule_id="high_latency",
            name="High Latency",
            description="Response time too high",
            metric_name="response_time",
            condition="greater_than",
            threshold_value=500.0,
            severity=AlertSeverity.HIGH,
            channels=[AlertChannel.LOG]
        )
        manager.add_alert_rule(rule)
        
        # Test metric that should trigger alert
        alerts = manager.evaluate_metric("response_time", 750.0)
        assert len(alerts) == 1
        assert alerts[0].rule_id == "high_latency"
        
        # Test metric that should not trigger alert
        alerts = manager.evaluate_metric("response_time", 250.0)
        # Should either be empty (no new alerts) or resolve existing ones
        assert isinstance(alerts, list)
    
    def test_alert_statistics(self):
        """Test alert statistics generation."""
        manager = AlertManager()
        
        stats = manager.get_alert_statistics()
        assert "total_rules" in stats
        assert "enabled_rules" in stats
        assert "active_alerts" in stats
        assert "severity_breakdown" in stats
        assert "configured_channels" in stats


class TestPhase2Integration:
    """Test integration between Phase 2 components."""
    
    @pytest.mark.asyncio
    async def test_health_and_performance_integration(self):
        """Test integration between health monitoring and performance tracking."""
        # Initialize components
        health_monitor = ComponentHealthMonitor()
        metrics_collector = MetricsCollector()
        
        # Record some performance metrics
        metrics_collector.record_performance_metric(
            component="database",
            operation="query",
            duration_ms=50.0,
            success=True
        )
        
        # Check component health
        health = await health_monitor.check_component_health("database")
        assert health.status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UNHEALTHY]
        
        # Both should report consistent information
        assert health.component_name == "database"
    
    def test_sla_and_alerting_integration(self):
        """Test integration between SLA tracking and alerting."""
        sla_tracker = SLATracker()
        alert_manager = AlertManager()
        
        # Calculate SLA metrics
        metrics = sla_tracker.calculate_sla_metrics("api")
        
        # Evaluate SLA breach alert
        if metrics.availability_percentage < 99.0:
            alerts = alert_manager.evaluate_metric(
                "api.availability",
                metrics.availability_percentage
            )
            assert isinstance(alerts, list)
    
    @pytest.mark.asyncio
    async def test_error_recovery_and_monitoring_integration(self):
        """Test integration between error recovery and monitoring."""
        recovery_manager = RecoveryManager()
        metrics_collector = MetricsCollector()
        
        # Simulate error and recovery
        error = TimeoutError("Service timeout")
        context = {"layer": "service", "operation": "request"}
        
        # Execute recovery
        result = await recovery_manager.execute_recovery("retry", error, context)
        
        # Record recovery metrics
        metrics_collector.record_performance_metric(
            component="recovery",
            operation="retry",
            duration_ms=100.0,
            success=(result == RecoveryResult.SUCCESS),
            metadata={"recovery_result": result.value}
        )
        
        # Verify metrics were recorded
        assert "recovery.retry" in metrics_collector.metrics
    
    def test_comprehensive_system_health(self):
        """Test comprehensive system health reporting."""
        # Initialize all monitoring components
        health_monitor = ComponentHealthMonitor()
        sla_tracker = SLATracker()
        metrics_collector = MetricsCollector()
        alert_manager = AlertManager()
        
        # Get overall system status
        overall_status, health_summary = health_monitor.get_overall_health_status()
        sla_summary = sla_tracker.get_sla_summary()
        metrics_summary = metrics_collector.get_metrics_summary()
        alert_stats = alert_manager.get_alert_statistics()
        
        # Verify all components return valid data
        assert overall_status in [HealthStatus.HEALTHY, HealthStatus.DEGRADED, HealthStatus.UNHEALTHY, HealthStatus.UNKNOWN]
        assert "overall_sla" in sla_summary
        assert "total_metrics" in metrics_summary
        assert "total_rules" in alert_stats
        
        # Comprehensive system health should include all components
        system_health = {
            "timestamp": datetime.utcnow().isoformat(),
            "overall_status": overall_status.value,
            "health_summary": health_summary,
            "sla_summary": sla_summary,
            "metrics_summary": metrics_summary,
            "alert_stats": alert_stats
        }
        
        assert "timestamp" in system_health
        assert "overall_status" in system_health
        assert len(system_health) >= 5  # Should have all major components


if __name__ == "__main__":
    # Run basic smoke tests
    import asyncio
    
    async def run_smoke_tests():
        print("🧪 Running Phase 2 smoke tests...")
        
        # Test error recovery
        print("Testing error recovery...")
        manager = RecoveryManager()
        error = TimeoutError("Test timeout")
        context = {"layer": "api", "operation": "test"}
        result = await manager.execute_recovery("retry", error, context)
        print(f"✅ Recovery test result: {result}")
        
        # Test health monitoring
        print("Testing health monitoring...")
        monitor = ComponentHealthMonitor()
        health = await monitor.check_component_health("database")
        print(f"✅ Database health: {health.status.value}")
        
        # Test SLA tracking
        print("Testing SLA tracking...")
        tracker = SLATracker()
        metrics = tracker.calculate_sla_metrics("api")
        print(f"✅ API SLA: {metrics.availability_percentage}%")
        
        # Test performance monitoring
        print("Testing performance monitoring...")
        collector = MetricsCollector()
        collector.record_performance_metric("api", "request", 120.0, True)
        summary = collector.get_metrics_summary()
        print(f"✅ Metrics collected: {summary['total_metrics']}")
        
        # Test alerting
        print("Testing alerting...")
        alert_mgr = AlertManager()
        stats = alert_mgr.get_alert_statistics()
        print(f"✅ Alert system initialized: {stats['configured_channels']}")
        
        print("🎉 All Phase 2 smoke tests passed!")
    
    asyncio.run(run_smoke_tests())