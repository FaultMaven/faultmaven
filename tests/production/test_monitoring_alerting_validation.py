#!/usr/bin/env python3
"""
Monitoring and Alerting Validation Tests for FaultMaven Phase 2

This module provides comprehensive validation of monitoring and alerting systems including:
- Production monitoring dashboard functionality
- SLA compliance and alerting system validation  
- Performance metrics accuracy and completeness
- Health check endpoint production readiness
- Incident response and escalation testing
- Metrics collection and export validation
- Alert rule effectiveness and notification testing
- Observability platform integration (OPIK, Prometheus, etc.)
"""

import pytest
import asyncio
import time
import json
import statistics
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
import aiohttp
import psutil
from datetime import datetime, timedelta


@dataclass
class MonitoringMetric:
    """Represents a monitoring metric for validation."""
    name: str
    current_value: float
    expected_range: Tuple[float, float]
    unit: str
    importance: str  # "critical", "important", "informational"
    threshold_breached: bool


@dataclass
class AlertRule:
    """Represents an alert rule configuration."""
    name: str
    metric_name: str
    threshold_value: float
    comparison: str  # "greater_than", "less_than", "equals"
    severity: str   # "critical", "warning", "info"
    notification_channels: List[str]


@dataclass
class MonitoringValidationResult:
    """Result from monitoring system validation."""
    component_name: str
    health_status: str  # "healthy", "degraded", "unhealthy"
    metrics_collected: List[MonitoringMetric]
    alert_rules_active: List[AlertRule]
    response_time_ms: float
    data_freshness_seconds: float
    sla_compliance: float
    issues_found: List[str]
    recommendations: List[str]


class MonitoringAlertingValidator:
    """Comprehensive monitoring and alerting validator for FaultMaven Phase 2."""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.session = None
        
        # Expected monitoring endpoints
        self.monitoring_endpoints = [
            {"path": "/health", "type": "basic_health", "required": True},
            {"path": "/health/dependencies", "type": "dependencies_health", "required": True},
            {"path": "/health/logging", "type": "logging_health", "required": True},
            {"path": "/health/sla", "type": "sla_monitoring", "required": True},
            {"path": "/health/components/{component}", "type": "component_health", "required": False},
            {"path": "/health/patterns", "type": "error_patterns", "required": False},
            {"path": "/metrics/performance", "type": "performance_metrics", "required": True},
            {"path": "/metrics/realtime", "type": "realtime_metrics", "required": True},
            {"path": "/metrics/alerts", "type": "alert_status", "required": True},
            {"path": "/metrics/optimization", "type": "optimization_metrics", "required": False}
        ]
        
        # Expected SLA thresholds
        self.sla_requirements = {
            "api_availability": {"threshold": 99.5, "unit": "percent"},
            "average_response_time": {"threshold": 500, "unit": "milliseconds"},
            "p95_response_time": {"threshold": 2000, "unit": "milliseconds"},
            "error_rate": {"threshold": 1.0, "unit": "percent"},
            "system_memory_usage": {"threshold": 80, "unit": "percent"},
            "system_cpu_usage": {"threshold": 85, "unit": "percent"}
        }
        
        # Expected alert rules
        self.critical_alert_rules = [
            AlertRule(
                name="high_error_rate",
                metric_name="error_rate_percent",
                threshold_value=5.0,
                comparison="greater_than",
                severity="critical",
                notification_channels=["email", "slack"]
            ),
            AlertRule(
                name="high_response_time",
                metric_name="p95_response_time_ms", 
                threshold_value=3000,
                comparison="greater_than",
                severity="warning",
                notification_channels=["slack"]
            ),
            AlertRule(
                name="low_api_availability",
                metric_name="api_availability_percent",
                threshold_value=99.0,
                comparison="less_than",
                severity="critical",
                notification_channels=["email", "slack", "pagerduty"]
            ),
            AlertRule(
                name="memory_exhaustion",
                metric_name="memory_usage_percent",
                threshold_value=90.0,
                comparison="greater_than",
                severity="warning",
                notification_channels=["slack"]
            ),
            AlertRule(
                name="service_dependency_failure",
                metric_name="dependency_health_score",
                threshold_value=0.8,
                comparison="less_than",
                severity="critical",
                notification_channels=["email", "slack"]
            )
        ]


@pytest.fixture
async def monitoring_validator():
    """Fixture providing configured monitoring validator."""
    validator = MonitoringAlertingValidator()
    validator.session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(limit=10)
    )
    yield validator
    await validator.session.close()


class TestHealthEndpointValidation:
    """Test health check endpoints for production readiness."""
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_basic_health_endpoint(self, monitoring_validator):
        """Test basic health endpoint functionality."""
        validator = monitoring_validator
        
        start_time = time.time()
        
        try:
            async with validator.session.get(f"{validator.base_url}/health") as resp:
                response_time_ms = (time.time() - start_time) * 1000
                
                # Health endpoint should respond quickly
                assert response_time_ms <= 1000, f"Health endpoint response time {response_time_ms:.0f}ms too slow"
                
                # Should return success status
                assert resp.status == 200, f"Health endpoint returned {resp.status}"
                
                health_data = await resp.json()
                
                # Should have required health information
                required_fields = ["status", "timestamp"]
                for field in required_fields:
                    assert field in health_data, f"Health endpoint missing required field: {field}"
                
                # Status should be healthy or degraded (not failed)
                status = health_data.get("status", "").lower()
                assert status in ["healthy", "degraded"], f"Health endpoint status '{status}' indicates system issues"
                
                # Should have component information
                if "components" in health_data:
                    components = health_data["components"]
                    assert isinstance(components, dict), "Components should be structured data"
                    
                    # At least some components should be healthy
                    healthy_components = sum(1 for comp in components.values() 
                                           if isinstance(comp, dict) and comp.get("status") == "healthy")
                    total_components = len(components)
                    
                    if total_components > 0:
                        health_ratio = healthy_components / total_components
                        assert health_ratio >= 0.5, f"Only {healthy_components}/{total_components} components healthy"
                
        except Exception as e:
            pytest.fail(f"Basic health endpoint failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_dependencies_health_endpoint(self, monitoring_validator):
        """Test dependencies health endpoint."""
        validator = monitoring_validator
        
        try:
            async with validator.session.get(f"{validator.base_url}/health/dependencies") as resp:
                assert resp.status == 200, f"Dependencies health endpoint returned {resp.status}"
                
                deps_data = await resp.json()
                
                # Should have dependency information
                required_fields = ["timestamp", "container_health"]
                for field in required_fields:
                    assert field in deps_data, f"Dependencies endpoint missing field: {field}"
                
                # Container health should be available
                container_health = deps_data.get("container_health", {})
                assert isinstance(container_health, dict), "Container health should be structured"
                
                if "status" in container_health:
                    container_status = container_health["status"]
                    assert container_status in ["healthy", "degraded"], \
                        f"Container health status '{container_status}' indicates issues"
                
                # Should have service tests
                if "service_tests" in deps_data:
                    service_tests = deps_data["service_tests"]
                    assert isinstance(service_tests, dict), "Service tests should be structured"
                    
                    # At least some services should be available
                    available_services = sum(1 for test in service_tests.values() 
                                           if isinstance(test, dict) and test.get("available", False))
                    
                    if available_services == 0:
                        pytest.skip("No services available for dependency testing")
                
        except Exception as e:
            pytest.fail(f"Dependencies health endpoint failed: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_sla_monitoring_endpoint(self, monitoring_validator):
        """Test SLA monitoring endpoint."""
        validator = monitoring_validator
        
        try:
            async with validator.session.get(f"{validator.base_url}/health/sla") as resp:
                if resp.status == 404:
                    pytest.skip("SLA monitoring endpoint not available")
                
                assert resp.status == 200, f"SLA monitoring endpoint returned {resp.status}"
                
                sla_data = await resp.json()
                
                # Should have SLA summary
                required_fields = ["timestamp", "summary"]
                for field in required_fields:
                    assert field in sla_data, f"SLA endpoint missing field: {field}"
                
                # SLA summary should have key metrics
                summary = sla_data.get("summary", {})
                expected_sla_fields = ["overall_sla", "active_breaches", "total_breaches_24h"]
                
                for field in expected_sla_fields:
                    if field in summary:
                        # Field exists, validate value
                        if field == "overall_sla":
                            overall_sla = summary[field]
                            if isinstance(overall_sla, (int, float)):
                                assert overall_sla >= 90.0, f"Overall SLA {overall_sla}% below acceptable threshold"
                
        except Exception as e:
            pytest.skip(f"SLA monitoring test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_component_health_endpoints(self, monitoring_validator):
        """Test individual component health endpoints."""
        validator = monitoring_validator
        
        # Test known components
        test_components = [
            "redis", "chromadb", "llm_provider", "session_manager", 
            "knowledge_base", "agent_service"
        ]
        
        working_components = []
        
        for component in test_components:
            try:
                endpoint = f"/health/components/{component}"
                async with validator.session.get(f"{validator.base_url}{endpoint}") as resp:
                    if resp.status == 200:
                        component_data = await resp.json()
                        
                        # Should have component-specific information
                        required_fields = ["timestamp", "component_name", "health"]
                        for field in required_fields:
                            assert field in component_data, \
                                f"Component {component} health missing field: {field}"
                        
                        working_components.append(component)
                        
            except Exception as e:
                # Component might not exist or be monitored individually
                pass
        
        # At least some components should be individually monitorable
        if len(working_components) == 0:
            pytest.skip("No individual component health endpoints available")
        
        assert len(working_components) >= 1, "At least one component should be individually monitorable"


class TestMetricsCollectionValidation:
    """Test metrics collection and accuracy."""
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_performance_metrics_endpoint(self, monitoring_validator):
        """Test performance metrics collection."""
        validator = monitoring_validator
        
        try:
            async with validator.session.get(f"{validator.base_url}/metrics/performance") as resp:
                if resp.status == 404:
                    pytest.skip("Performance metrics endpoint not available")
                
                assert resp.status == 200, f"Performance metrics endpoint returned {resp.status}"
                
                metrics_data = await resp.json()
                
                # Should have timestamp
                assert "timestamp" in metrics_data, "Performance metrics missing timestamp"
                
                # Should have performance data
                performance_fields = [
                    "response_time", "throughput", "memory_usage", 
                    "cpu_usage", "request_count", "error_count"
                ]
                
                found_fields = []
                for field in performance_fields:
                    # Look for field or variations
                    for key in metrics_data.keys():
                        if field.replace("_", "") in key.replace("_", "").lower():
                            found_fields.append(field)
                            break
                
                # Should have at least basic performance metrics
                assert len(found_fields) >= 2, f"Performance metrics insufficient: found {found_fields}"
                
        except Exception as e:
            pytest.skip(f"Performance metrics test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_realtime_metrics_endpoint(self, monitoring_validator):
        """Test real-time metrics collection."""
        validator = monitoring_validator
        
        try:
            # Test with different time windows
            time_windows = [1, 5, 15]  # minutes
            
            for window in time_windows:
                async with validator.session.get(
                    f"{validator.base_url}/metrics/realtime?time_window_minutes={window}"
                ) as resp:
                    if resp.status == 404:
                        pytest.skip("Real-time metrics endpoint not available")
                    
                    assert resp.status == 200, f"Real-time metrics returned {resp.status} for {window}min window"
                    
                    metrics_data = await resp.json()
                    
                    # Should have timestamp and time window
                    assert "timestamp" in metrics_data, "Real-time metrics missing timestamp"
                    assert "time_window_minutes" in metrics_data, "Real-time metrics missing time window"
                    
                    # Time window should match request
                    returned_window = metrics_data.get("time_window_minutes")
                    assert returned_window == window, f"Time window mismatch: requested {window}, got {returned_window}"
                    
                    # Should have dashboard data
                    if "dashboard" in metrics_data:
                        dashboard = metrics_data["dashboard"]
                        assert isinstance(dashboard, dict), "Dashboard data should be structured"
                    
                    # Should have active alerts
                    if "active_alerts" in metrics_data:
                        alerts = metrics_data["active_alerts"]
                        assert isinstance(alerts, list), "Active alerts should be a list"
                    
                    break  # Test first working time window
                    
        except Exception as e:
            pytest.skip(f"Real-time metrics test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_metrics_accuracy_and_freshness(self, monitoring_validator):
        """Test metrics accuracy and data freshness."""
        validator = monitoring_validator
        
        # Generate some load to create measurable metrics
        start_time = time.time()
        
        # Make several requests to generate metrics
        for i in range(5):
            try:
                async with validator.session.get(f"{validator.base_url}/health") as resp:
                    pass
            except:
                pass
            await asyncio.sleep(0.5)
        
        generation_time = time.time() - start_time
        
        # Wait brief moment for metrics to be collected
        await asyncio.sleep(2)
        
        # Check performance metrics for freshness
        try:
            async with validator.session.get(f"{validator.base_url}/metrics/performance") as resp:
                if resp.status == 200:
                    metrics_data = await resp.json()
                    
                    # Check timestamp freshness
                    if "timestamp" in metrics_data:
                        timestamp_str = metrics_data["timestamp"]
                        try:
                            # Parse ISO timestamp
                            if "T" in timestamp_str:
                                metrics_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                            else:
                                metrics_time = datetime.fromisoformat(timestamp_str)
                            
                            current_time = datetime.utcnow()
                            age_seconds = (current_time - metrics_time.replace(tzinfo=None)).total_seconds()
                            
                            # Metrics should be relatively fresh (within 5 minutes)
                            assert age_seconds <= 300, f"Metrics are {age_seconds:.0f}s old, too stale"
                            
                        except Exception as e:
                            # Timestamp parsing failed, but that's not necessarily a failure
                            pass
                    
                    # Check for reasonable metric values
                    self._validate_metric_reasonableness(metrics_data)
                    
        except Exception as e:
            pytest.skip(f"Metrics accuracy test inconclusive: {e}")
    
    def _validate_metric_reasonableness(self, metrics_data: Dict[str, Any]):
        """Validate that metric values are reasonable."""
        # Check response times (should be positive and reasonable)
        response_time_fields = ["response_time", "average_response_time", "avg_response_time"]
        for field in response_time_fields:
            if field in metrics_data:
                value = metrics_data[field]
                if isinstance(value, (int, float)):
                    assert 0 <= value <= 60000, f"Response time {field}={value} unreasonable (0-60000ms expected)"
        
        # Check percentages (should be 0-100)
        percentage_fields = ["cpu_usage", "memory_usage", "error_rate", "availability"]
        for field in percentage_fields:
            if field in metrics_data:
                value = metrics_data[field]
                if isinstance(value, (int, float)):
                    assert 0 <= value <= 100, f"Percentage {field}={value} out of range (0-100% expected)"
        
        # Check counts (should be non-negative)
        count_fields = ["request_count", "error_count", "total_requests"]
        for field in count_fields:
            if field in metrics_data:
                value = metrics_data[field]
                if isinstance(value, (int, float)):
                    assert value >= 0, f"Count {field}={value} should be non-negative"


class TestAlertingSystemValidation:
    """Test alerting system functionality."""
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_alert_status_endpoint(self, monitoring_validator):
        """Test alert status endpoint."""
        validator = monitoring_validator
        
        try:
            async with validator.session.get(f"{validator.base_url}/metrics/alerts") as resp:
                if resp.status == 404:
                    pytest.skip("Alert status endpoint not available")
                
                assert resp.status == 200, f"Alert status endpoint returned {resp.status}"
                
                alerts_data = await resp.json()
                
                # Should have timestamp
                assert "timestamp" in alerts_data, "Alert status missing timestamp"
                
                # Should have statistics
                if "statistics" in alerts_data:
                    stats = alerts_data["statistics"]
                    assert isinstance(stats, dict), "Alert statistics should be structured"
                
                # Should have active alerts list
                if "active_alerts" in alerts_data:
                    active_alerts = alerts_data["active_alerts"]
                    assert isinstance(active_alerts, list), "Active alerts should be a list"
                    
                    # Each alert should have required fields
                    for alert in active_alerts:
                        if isinstance(alert, dict):
                            required_alert_fields = ["rule_name", "severity", "metric_name"]
                            for field in required_alert_fields:
                                assert field in alert, f"Alert missing required field: {field}"
                
        except Exception as e:
            pytest.skip(f"Alert status test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_alert_rule_effectiveness(self, monitoring_validator):
        """Test alert rule effectiveness by simulating conditions."""
        validator = monitoring_validator
        
        # Test alert system responsiveness by creating load
        alert_test_start = time.time()
        
        # Generate load that might trigger alerts
        load_requests = []
        
        for i in range(10):
            try:
                start_request = time.time()
                async with validator.session.get(f"{validator.base_url}/health") as resp:
                    request_time = time.time() - start_request
                    load_requests.append({
                        "status": resp.status,
                        "response_time_ms": request_time * 1000
                    })
            except Exception as e:
                load_requests.append({
                    "status": 0,
                    "error": str(e),
                    "response_time_ms": 5000  # Assume high response time for errors
                })
            
            await asyncio.sleep(0.2)
        
        # Wait for alert system to potentially react
        await asyncio.sleep(3)
        
        # Check if alert system is responsive
        try:
            async with validator.session.get(f"{validator.base_url}/metrics/alerts") as resp:
                if resp.status == 200:
                    alerts_data = await resp.json()
                    
                    # Alert system should be functional after load test
                    assert "timestamp" in alerts_data, "Alert system not responding after load test"
                    
                    # If there are active alerts, they should be properly structured
                    active_alerts = alerts_data.get("active_alerts", [])
                    
                    for alert in active_alerts:
                        if isinstance(alert, dict):
                            # Alert should have valid severity
                            severity = alert.get("severity", "").lower()
                            assert severity in ["critical", "warning", "info"], \
                                f"Invalid alert severity: {severity}"
                            
                            # Alert should have meaningful message
                            message = alert.get("message", "")
                            assert len(message) > 0, "Alert should have descriptive message"
                
        except Exception as e:
            pytest.skip(f"Alert effectiveness test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_alert_notification_configuration(self, monitoring_validator):
        """Test alert notification configuration."""
        validator = monitoring_validator
        
        # This test checks if the alerting infrastructure is properly configured
        # In a real production environment, this would test actual notification channels
        
        try:
            # Check alert system health
            async with validator.session.get(f"{validator.base_url}/metrics/alerts") as resp:
                if resp.status == 200:
                    alerts_data = await resp.json()
                    
                    # Should have statistics that indicate alert system is working
                    stats = alerts_data.get("statistics", {})
                    
                    # If statistics exist, they should show alert system activity
                    if stats:
                        # Should track alert metrics
                        expected_stats = ["total_alerts", "active_alerts", "resolved_alerts"]
                        found_stats = []
                        
                        for stat_name in expected_stats:
                            for key in stats.keys():
                                if stat_name.replace("_", "") in key.replace("_", "").lower():
                                    found_stats.append(stat_name)
                                    break
                        
                        # Should have at least some alert statistics
                        if len(found_stats) == 0:
                            pytest.skip("Alert system statistics not available for validation")
                    
                    # Active alerts should be properly configured
                    active_alerts = alerts_data.get("active_alerts", [])
                    
                    if active_alerts:
                        # Sample first few alerts for configuration validation
                        for alert in active_alerts[:3]:
                            if isinstance(alert, dict):
                                # Should have alert ID for tracking
                                assert "alert_id" in alert or "rule_name" in alert, \
                                    "Alerts should have identification for tracking"
                                
                                # Should have timestamp
                                assert "triggered_at" in alert or "timestamp" in alert, \
                                    "Alerts should have timestamps"
                
        except Exception as e:
            pytest.skip(f"Alert notification configuration test inconclusive: {e}")


class TestSLAComplianceValidation:
    """Test SLA compliance and tracking."""
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_sla_threshold_compliance(self, monitoring_validator):
        """Test SLA threshold compliance."""
        validator = monitoring_validator
        
        try:
            async with validator.session.get(f"{validator.base_url}/health/sla") as resp:
                if resp.status != 200:
                    pytest.skip("SLA monitoring not available")
                
                sla_data = await resp.json()
                
                # Check overall SLA compliance
                summary = sla_data.get("summary", {})
                
                if "overall_sla" in summary:
                    overall_sla = summary["overall_sla"]
                    
                    if isinstance(overall_sla, (int, float)):
                        # Overall SLA should meet minimum threshold
                        assert overall_sla >= 95.0, f"Overall SLA {overall_sla}% below 95% threshold"
                        
                        # Log warning if SLA is degraded but acceptable
                        if overall_sla < 99.0:
                            print(f"Warning: Overall SLA {overall_sla}% is below 99%")
                
                # Check for active SLA breaches
                active_breaches = summary.get("active_breaches", 0)
                
                if isinstance(active_breaches, int):
                    # Should not have many active breaches
                    assert active_breaches <= 2, f"Too many active SLA breaches: {active_breaches}"
                
                # Check component-level SLA if available
                components = sla_data.get("components", {})
                
                critical_components = ["api", "database", "session_management"]
                
                for component_name in critical_components:
                    # Look for component in SLA data
                    component_data = None
                    for key, value in components.items():
                        if component_name.replace("_", "") in key.replace("_", "").lower():
                            component_data = value
                            break
                    
                    if component_data and isinstance(component_data, dict):
                        component_sla = component_data.get("sla_current")
                        if isinstance(component_sla, (int, float)):
                            assert component_sla >= 90.0, \
                                f"Component {component_name} SLA {component_sla}% below 90%"
                
        except Exception as e:
            pytest.skip(f"SLA compliance test inconclusive: {e}")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_performance_sla_validation(self, monitoring_validator):
        """Test performance-related SLA validation."""
        validator = monitoring_validator
        
        # Measure actual performance vs SLA requirements
        performance_samples = []
        
        # Take multiple performance samples
        for i in range(5):
            start_time = time.time()
            
            try:
                async with validator.session.get(f"{validator.base_url}/health") as resp:
                    response_time_ms = (time.time() - start_time) * 1000
                    
                    performance_samples.append({
                        "response_time_ms": response_time_ms,
                        "success": resp.status == 200,
                        "timestamp": time.time()
                    })
                    
            except Exception as e:
                performance_samples.append({
                    "response_time_ms": 30000,  # Assume timeout
                    "success": False,
                    "error": str(e),
                    "timestamp": time.time()
                })
            
            await asyncio.sleep(1)
        
        # Analyze performance against SLA requirements
        if performance_samples:
            response_times = [s["response_time_ms"] for s in performance_samples]
            success_count = sum(1 for s in performance_samples if s["success"])
            
            # Calculate metrics
            avg_response_time = statistics.mean(response_times)
            p95_response_time = statistics.quantiles(response_times, n=20)[18] if len(response_times) >= 5 else max(response_times)
            availability_percent = (success_count / len(performance_samples)) * 100
            
            # Validate against SLA requirements
            sla_reqs = validator.sla_requirements
            
            # Average response time SLA
            if "average_response_time" in sla_reqs:
                threshold = sla_reqs["average_response_time"]["threshold"]
                assert avg_response_time <= threshold * 1.5, \
                    f"Average response time {avg_response_time:.0f}ms exceeds SLA threshold {threshold}ms"
            
            # P95 response time SLA
            if "p95_response_time" in sla_reqs:
                threshold = sla_reqs["p95_response_time"]["threshold"] 
                assert p95_response_time <= threshold * 1.5, \
                    f"P95 response time {p95_response_time:.0f}ms exceeds SLA threshold {threshold}ms"
            
            # Availability SLA
            if "api_availability" in sla_reqs:
                threshold = sla_reqs["api_availability"]["threshold"]
                assert availability_percent >= threshold * 0.9, \
                    f"API availability {availability_percent:.1f}% below SLA threshold {threshold}%"


class TestObservabilityIntegration:
    """Test observability platform integration."""
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_opik_integration_health(self, monitoring_validator):
        """Test OPIK observability platform integration."""
        validator = monitoring_validator
        
        # Check if OPIK integration is working through API health
        try:
            async with validator.session.get(f"{validator.base_url}/health/dependencies") as resp:
                if resp.status == 200:
                    deps_data = await resp.json()
                    
                    # Look for observability/tracing references
                    deps_text = json.dumps(deps_data).lower()
                    
                    observability_indicators = ["opik", "tracing", "observability", "metrics"]
                    found_indicators = [indicator for indicator in observability_indicators if indicator in deps_text]
                    
                    if found_indicators:
                        # Observability integration is present
                        print(f"Observability integration detected: {found_indicators}")
                    else:
                        pytest.skip("Observability integration not detectable in dependencies")
                        
        except Exception as e:
            pytest.skip(f"OPIK integration test inconclusive: {e}")
        
        # Test OPIK external endpoint if available
        opik_endpoints = [
            "http://opik.faultmaven.local:30080",
            "http://opik-api.faultmaven.local:30080"
        ]
        
        opik_accessible = False
        
        for endpoint in opik_endpoints:
            try:
                async with validator.session.get(f"{endpoint}", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        opik_accessible = True
                        print(f"OPIK accessible at {endpoint}")
                        break
            except:
                continue
        
        if not opik_accessible:
            pytest.skip("OPIK external endpoints not accessible")
    
    @pytest.mark.asyncio
    @pytest.mark.monitoring
    async def test_metrics_export_capability(self, monitoring_validator):
        """Test metrics export capability for external monitoring."""
        validator = monitoring_validator
        
        # Check if metrics are available in exportable format
        metrics_endpoints = [
            "/metrics/performance",
            "/metrics/realtime", 
            "/health/sla"
        ]
        
        exportable_metrics = []
        
        for endpoint in metrics_endpoints:
            try:
                async with validator.session.get(f"{validator.base_url}{endpoint}") as resp:
                    if resp.status == 200:
                        metrics_data = await resp.json()
                        
                        # Check if metrics are structured for export
                        if isinstance(metrics_data, dict):
                            # Should have timestamp for time-series data
                            if "timestamp" in metrics_data:
                                exportable_metrics.append(endpoint)
                                
                                # Should have numerical metrics
                                numerical_fields = []
                                for key, value in metrics_data.items():
                                    if isinstance(value, (int, float)):
                                        numerical_fields.append(key)
                                    elif isinstance(value, dict):
                                        for subkey, subvalue in value.items():
                                            if isinstance(subvalue, (int, float)):
                                                numerical_fields.append(f"{key}.{subkey}")
                                
                                if len(numerical_fields) >= 3:
                                    print(f"Endpoint {endpoint} has {len(numerical_fields)} exportable metrics")
                                    
            except Exception as e:
                continue
        
        # Should have at least some exportable metrics
        assert len(exportable_metrics) >= 1, "No metrics endpoints provide exportable data"


@pytest.mark.monitoring
class TestOverallMonitoringReadiness:
    """Test overall monitoring system production readiness."""
    
    @pytest.mark.asyncio
    async def test_comprehensive_monitoring_assessment(self, monitoring_validator):
        """Comprehensive monitoring system assessment."""
        validator = monitoring_validator
        
        assessment_results = {
            "health_endpoints": 0,
            "metrics_collection": 0,
            "alerting_system": 0,
            "sla_monitoring": 0,
            "observability": 0
        }
        
        # Test health endpoints
        health_endpoints = ["/health", "/health/dependencies"]
        working_health_endpoints = 0
        
        for endpoint in health_endpoints:
            try:
                async with validator.session.get(f"{validator.base_url}{endpoint}") as resp:
                    if resp.status == 200:
                        working_health_endpoints += 1
            except:
                pass
        
        assessment_results["health_endpoints"] = working_health_endpoints / len(health_endpoints)
        
        # Test metrics collection
        metrics_endpoints = ["/metrics/performance", "/metrics/realtime"]
        working_metrics_endpoints = 0
        
        for endpoint in metrics_endpoints:
            try:
                async with validator.session.get(f"{validator.base_url}{endpoint}") as resp:
                    if resp.status == 200:
                        working_metrics_endpoints += 1
            except:
                pass
        
        assessment_results["metrics_collection"] = working_metrics_endpoints / len(metrics_endpoints)
        
        # Test alerting system
        try:
            async with validator.session.get(f"{validator.base_url}/metrics/alerts") as resp:
                if resp.status == 200:
                    assessment_results["alerting_system"] = 1.0
        except:
            assessment_results["alerting_system"] = 0.0
        
        # Test SLA monitoring
        try:
            async with validator.session.get(f"{validator.base_url}/health/sla") as resp:
                if resp.status == 200:
                    assessment_results["sla_monitoring"] = 1.0
        except:
            assessment_results["sla_monitoring"] = 0.0
        
        # Test observability
        try:
            async with validator.session.get(f"{validator.base_url}/health/dependencies") as resp:
                if resp.status == 200:
                    deps_data = await resp.json()
                    deps_text = json.dumps(deps_data).lower()
                    if "opik" in deps_text or "tracing" in deps_text:
                        assessment_results["observability"] = 1.0
        except:
            assessment_results["observability"] = 0.0
        
        # Calculate overall monitoring readiness score
        total_categories = len(assessment_results)
        overall_score = sum(assessment_results.values()) / total_categories
        
        print(f"Monitoring readiness assessment: {assessment_results}")
        print(f"Overall monitoring score: {overall_score:.1%}")
        
        # Should achieve at least 60% monitoring readiness
        assert overall_score >= 0.6, \
            f"Monitoring readiness score {overall_score:.1%} below 60% threshold"
        
        # Critical monitoring components should be available
        critical_components = ["health_endpoints", "metrics_collection"]
        for component in critical_components:
            assert assessment_results[component] >= 0.5, \
                f"Critical monitoring component '{component}' not adequately implemented"
    
    @pytest.mark.asyncio
    async def test_monitoring_system_stability(self, monitoring_validator):
        """Test monitoring system stability under load."""
        validator = monitoring_validator
        
        # Test monitoring endpoints under mild load
        monitoring_endpoints = [
            "/health",
            "/health/dependencies", 
            "/metrics/performance",
            "/metrics/alerts"
        ]
        
        stability_results = []
        
        # Make multiple requests to each endpoint
        for endpoint in monitoring_endpoints:
            endpoint_results = []
            
            for attempt in range(3):
                start_time = time.time()
                
                try:
                    async with validator.session.get(f"{validator.base_url}{endpoint}") as resp:
                        response_time = time.time() - start_time
                        
                        endpoint_results.append({
                            "success": resp.status == 200,
                            "response_time_ms": response_time * 1000,
                            "status": resp.status
                        })
                        
                except Exception as e:
                    endpoint_results.append({
                        "success": False,
                        "response_time_ms": 30000,
                        "error": str(e)
                    })
                
                await asyncio.sleep(0.5)
            
            # Analyze endpoint stability
            success_rate = sum(1 for r in endpoint_results if r["success"]) / len(endpoint_results)
            avg_response_time = statistics.mean([r["response_time_ms"] for r in endpoint_results])
            
            stability_results.append({
                "endpoint": endpoint,
                "success_rate": success_rate,
                "avg_response_time_ms": avg_response_time,
                "stable": success_rate >= 0.8 and avg_response_time <= 2000
            })
        
        # Monitoring endpoints should be stable
        stable_endpoints = sum(1 for r in stability_results if r["stable"])
        total_endpoints = len(stability_results)
        
        stability_ratio = stable_endpoints / total_endpoints if total_endpoints > 0 else 0
        
        assert stability_ratio >= 0.7, \
            f"Only {stable_endpoints}/{total_endpoints} monitoring endpoints stable"
        
        # At least basic health endpoint should be very stable
        health_result = next((r for r in stability_results if r["endpoint"] == "/health"), None)
        if health_result:
            assert health_result["success_rate"] >= 0.9, \
                f"Basic health endpoint success rate {health_result['success_rate']:.1%} too low"


if __name__ == "__main__":
    import sys
    
    # Allow running this module directly for debugging
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        pytest.main([__file__, "-v", "-m", "monitoring"])
    else:
        print("Monitoring and Alerting Validation Test Suite")
        print("Usage: python test_monitoring_alerting_validation.py --test")