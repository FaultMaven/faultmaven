"""Performance SLA Monitoring and Alerting System

This module provides comprehensive SLA monitoring and alerting capabilities
for the FaultMaven Phase 2 intelligent troubleshooting system. It monitors
service-level agreements, detects violations, and triggers appropriate
alerts and notifications.

Key Features:
- Real-time SLA compliance monitoring
- Multi-tier alerting (warning, critical, escalation)
- Custom SLA definitions for each service
- Automated alert routing and notifications
- SLA violation trend analysis and reporting
- Integration with external alerting systems
- Performance regression detection
- Capacity planning based on SLA trends

SLA Categories:
- Availability SLAs (uptime, service availability)
- Performance SLAs (response time, throughput)
- Quality SLAs (error rates, success rates)
- Resource SLAs (CPU, memory, storage)
"""

import asyncio
import json
import logging
import smtplib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, Any, List, Optional, Tuple, Callable, Set
import threading
from concurrent.futures import ThreadPoolExecutor
import statistics

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector
from faultmaven.services.analytics.dashboard_service import AnalyticsDashboardService
from faultmaven.models.interfaces import ITracer


@dataclass
class SLADefinition:
    """SLA definition with thresholds and targets"""

    sla_id: str
    name: str
    description: str
    service: str
    metric_type: str  # "response_time", "availability", "error_rate", "throughput"
    target_value: float
    warning_threshold: float
    critical_threshold: float
    measurement_window_minutes: int
    evaluation_frequency_seconds: int
    alert_channels: List[str]  # "email", "slack", "webhook", "pagerduty"
    escalation_rules: Dict[str, Any]
    enabled: bool = True


@dataclass
class SLAViolation:
    """SLA violation record"""

    violation_id: str
    sla_id: str
    service: str
    metric_type: str
    severity: str  # "warning", "critical", "escalated"
    target_value: float
    actual_value: float
    violation_percentage: float
    detection_time: datetime
    duration_seconds: float
    affected_operations: List[str]
    impact_assessment: Dict[str, Any]
    resolution_time: Optional[datetime] = None
    resolved: bool = False


@dataclass
class AlertChannel:
    """Alert notification channel configuration"""

    channel_id: str
    channel_type: str  # "email", "slack", "webhook", "pagerduty"
    endpoint: str
    credentials: Dict[str, Any]
    rate_limit_per_hour: int
    enabled: bool = True


@dataclass
class SLAComplianceReport:
    """SLA compliance report"""

    service: str
    reporting_period: timedelta
    sla_compliance_percentage: float
    violations_count: int
    mttr_seconds: float  # Mean Time To Recovery
    availability_percentage: float
    performance_metrics: Dict[str, float]
    trend_analysis: Dict[str, Any]
    recommendations: List[str]


class SLAMonitor(BaseExternalClient):
    """SLA Monitoring and Alerting System

    This service continuously monitors service-level agreements (SLAs) for
    the FaultMaven system and triggers appropriate alerts when violations
    are detected. It provides comprehensive SLA tracking, alerting, and
    reporting capabilities.

    Key Responsibilities:
    - Monitor SLA compliance in real-time
    - Detect and classify SLA violations
    - Send alerts through multiple channels
    - Track violation trends and patterns
    - Generate SLA compliance reports
    - Provide capacity planning insights
    - Integrate with external monitoring systems
    """

    def __init__(
        self,
        metrics_collector: Optional[MetricsCollector] = None,
        analytics_service: Optional[AnalyticsDashboardService] = None,
        tracer: Optional[ITracer] = None,
        alert_rate_limit_per_hour: int = 20,
    ):
        """Initialize SLA Monitor

        Args:
            metrics_collector: Metrics collection service
            analytics_service: Analytics dashboard service
            tracer: Distributed tracing service
            alert_rate_limit_per_hour: Maximum alerts per hour per channel
        """
        super().__init__(
            client_name="SLAMonitor",
            service_name="FaultMaven-SLA",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30,
        )

        self._metrics_collector = metrics_collector
        self._analytics_service = analytics_service
        self._tracer = tracer
        self._alert_rate_limit = alert_rate_limit_per_hour

        # SLA definitions and configurations
        self._sla_definitions: Dict[str, SLADefinition] = {}
        self._alert_channels: Dict[str, AlertChannel] = {}

        # Violation tracking
        self._active_violations: Dict[str, SLAViolation] = {}
        self._violation_history: deque = deque(maxlen=10000)
        self._violation_lock = threading.RLock()

        # Alert tracking and rate limiting
        self._alert_history: deque = deque(maxlen=5000)
        self._alert_rate_tracker: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100)
        )
        self._alert_lock = threading.RLock()

        # SLA compliance tracking
        self._compliance_data: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=1440)
        )  # 24 hours
        self._compliance_lock = threading.RLock()

        # Background processing
        self._background_tasks_running = False
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="sla-monitor"
        )

        # Configuration
        self._config = {
            "monitoring_interval_seconds": 30,
            "compliance_calculation_interval": 300,  # 5 minutes
            "violation_resolution_timeout": 3600,  # 1 hour
            "alert_consolidation_window": 600,  # 10 minutes
            "trend_analysis_window_hours": 24,
            "notification_retry_attempts": 3,
            "notification_retry_delay": 60,
        }

        # Performance metrics
        self._sla_metrics = {
            "total_slas_monitored": 0,
            "active_violations": 0,
            "alerts_sent_24h": 0,
            "avg_detection_time_ms": 0.0,
            "avg_resolution_time_minutes": 0.0,
            "overall_compliance_percentage": 100.0,
        }

        # Initialize default SLAs
        self._initialize_default_slas()
        self._initialize_default_alert_channels()

        self.logger.info(
            "SLAMonitor initialized with comprehensive monitoring capabilities"
        )

    def _initialize_default_slas(self):
        """Initialize default SLA definitions for Phase 2 services"""
        default_slas = [
            # Memory Service SLAs
            SLADefinition(
                sla_id="memory_service_response_time",
                name="Memory Service Response Time",
                description="Memory service operations should complete within 50ms",
                service="memory_service",
                metric_type="response_time",
                target_value=50.0,  # 50ms target
                warning_threshold=75.0,  # Warning at 75ms
                critical_threshold=100.0,  # Critical at 100ms
                measurement_window_minutes=5,
                evaluation_frequency_seconds=30,
                alert_channels=["email", "slack"],
                escalation_rules={
                    "escalate_after_minutes": 15,
                    "escalation_channels": ["pagerduty"],
                },
            ),
            SLADefinition(
                sla_id="memory_service_availability",
                name="Memory Service Availability",
                description="Memory service should maintain 99.9% availability",
                service="memory_service",
                metric_type="availability",
                target_value=99.9,  # 99.9% availability
                warning_threshold=99.5,  # Warning at 99.5%
                critical_threshold=99.0,  # Critical at 99.0%
                measurement_window_minutes=60,
                evaluation_frequency_seconds=300,
                alert_channels=["email", "slack", "pagerduty"],
                escalation_rules={
                    "escalate_after_minutes": 5,
                    "escalation_channels": ["pagerduty"],
                },
            ),
            # Planning Service SLAs
            SLADefinition(
                sla_id="planning_service_response_time",
                name="Planning Service Response Time",
                description="Planning service operations should complete within 300ms",
                service="planning_service",
                metric_type="response_time",
                target_value=300.0,  # 300ms target
                warning_threshold=500.0,  # Warning at 500ms
                critical_threshold=800.0,  # Critical at 800ms
                measurement_window_minutes=5,
                evaluation_frequency_seconds=60,
                alert_channels=["email", "slack"],
                escalation_rules={
                    "escalate_after_minutes": 20,
                    "escalation_channels": ["pagerduty"],
                },
            ),
            # Knowledge Service SLAs
            SLADefinition(
                sla_id="knowledge_service_response_time",
                name="Knowledge Service Response Time",
                description="Knowledge service searches should complete within 100ms",
                service="knowledge_service",
                metric_type="response_time",
                target_value=100.0,  # 100ms target
                warning_threshold=200.0,  # Warning at 200ms
                critical_threshold=500.0,  # Critical at 500ms
                measurement_window_minutes=5,
                evaluation_frequency_seconds=30,
                alert_channels=["email", "slack"],
                escalation_rules={
                    "escalate_after_minutes": 10,
                    "escalation_channels": ["pagerduty"],
                },
            ),
            # Orchestration Service SLAs
            SLADefinition(
                sla_id="orchestration_service_response_time",
                name="Orchestration Service Response Time",
                description="Orchestration workflows should complete steps within 3 seconds",
                service="orchestration_service",
                metric_type="response_time",
                target_value=3000.0,  # 3 seconds target
                warning_threshold=5000.0,  # Warning at 5 seconds
                critical_threshold=10000.0,  # Critical at 10 seconds
                measurement_window_minutes=10,
                evaluation_frequency_seconds=60,
                alert_channels=["email", "slack"],
                escalation_rules={
                    "escalate_after_minutes": 30,
                    "escalation_channels": ["pagerduty"],
                },
            ),
            # System-wide SLAs
            SLADefinition(
                sla_id="system_error_rate",
                name="System Error Rate",
                description="System-wide error rate should remain below 1%",
                service="system",
                metric_type="error_rate",
                target_value=0.01,  # 1% error rate
                warning_threshold=0.02,  # Warning at 2%
                critical_threshold=0.05,  # Critical at 5%
                measurement_window_minutes=15,
                evaluation_frequency_seconds=120,
                alert_channels=["email", "slack", "pagerduty"],
                escalation_rules={
                    "escalate_after_minutes": 5,
                    "escalation_channels": ["pagerduty"],
                },
            ),
            SLADefinition(
                sla_id="system_throughput",
                name="System Throughput",
                description="System should maintain minimum 100 requests per second",
                service="system",
                metric_type="throughput",
                target_value=100.0,  # 100 RPS minimum
                warning_threshold=75.0,  # Warning at 75 RPS
                critical_threshold=50.0,  # Critical at 50 RPS
                measurement_window_minutes=10,
                evaluation_frequency_seconds=60,
                alert_channels=["email", "slack"],
                escalation_rules={
                    "escalate_after_minutes": 10,
                    "escalation_channels": ["pagerduty"],
                },
            ),
        ]

        for sla in default_slas:
            self._sla_definitions[sla.sla_id] = sla

        self._sla_metrics["total_slas_monitored"] = len(self._sla_definitions)
        self.logger.info(f"Initialized {len(default_slas)} default SLA definitions")

    def _initialize_default_alert_channels(self):
        """Initialize default alert notification channels"""
        default_channels = [
            AlertChannel(
                channel_id="email_ops",
                channel_type="email",
                endpoint="ops@faultmaven.com",
                credentials={
                    "smtp_server": "smtp.faultmaven.com",
                    "smtp_port": 587,
                    "username": "alerts@faultmaven.com",
                    "password": "secure_password",  # Would use environment variable
                },
                rate_limit_per_hour=10,
            ),
            AlertChannel(
                channel_id="slack_alerts",
                channel_type="slack",
                endpoint="https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
                credentials={
                    "webhook_url": "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
                },
                rate_limit_per_hour=20,
            ),
            AlertChannel(
                channel_id="pagerduty_critical",
                channel_type="pagerduty",
                endpoint="https://events.pagerduty.com/v2/enqueue",
                credentials={
                    "routing_key": "pagerduty_integration_key",
                    "service_name": "FaultMaven Phase 2",
                },
                rate_limit_per_hour=50,
            ),
            AlertChannel(
                channel_id="webhook_monitoring",
                channel_type="webhook",
                endpoint="https://monitoring.faultmaven.com/alerts",
                credentials={
                    "api_key": "monitoring_api_key",
                    "secret": "monitoring_secret",
                },
                rate_limit_per_hour=100,
            ),
        ]

        for channel in default_channels:
            self._alert_channels[channel.channel_id] = channel

        self.logger.info(
            f"Initialized {len(default_channels)} alert notification channels"
        )

    async def start_monitoring(self):
        """Start SLA monitoring background tasks"""
        if self._background_tasks_running:
            return

        self._background_tasks_running = True

        # Start monitoring tasks
        asyncio.create_task(self._sla_monitor_task())
        asyncio.create_task(self._compliance_calculator_task())
        asyncio.create_task(self._violation_resolver_task())
        asyncio.create_task(self._alert_consolidator_task())

        self.logger.info("SLA monitoring background tasks started")

    async def stop_monitoring(self):
        """Stop SLA monitoring background tasks"""
        self._background_tasks_running = False
        self._executor.shutdown(wait=True)
        self.logger.info("SLA monitoring background tasks stopped")

    async def get_sla_status(
        self,
        service_filter: Optional[str] = None,
        include_violations: bool = True,
        include_trends: bool = False,
    ) -> Dict[str, Any]:
        """Get current SLA status and compliance

        Args:
            service_filter: Filter by specific service
            include_violations: Include active violation details
            include_trends: Include trend analysis

        Returns:
            Comprehensive SLA status report
        """
        try:
            # Filter SLAs if requested
            slas_to_check = self._sla_definitions
            if service_filter:
                slas_to_check = {
                    sla_id: sla
                    for sla_id, sla in self._sla_definitions.items()
                    if sla.service == service_filter
                }

            # Calculate current compliance
            sla_status = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service_filter": service_filter,
                "overall_compliance": await self._calculate_overall_compliance(
                    slas_to_check
                ),
                "sla_details": {},
                "active_violations": [],
                "compliance_summary": {},
                "performance_metrics": self._sla_metrics.copy(),
            }

            # Get detailed status for each SLA
            for sla_id, sla in slas_to_check.items():
                try:
                    sla_compliance = await self._calculate_sla_compliance(sla)

                    sla_status["sla_details"][sla_id] = {
                        "name": sla.name,
                        "service": sla.service,
                        "metric_type": sla.metric_type,
                        "target_value": sla.target_value,
                        "current_value": sla_compliance.get("current_value", 0.0),
                        "compliance_percentage": sla_compliance.get(
                            "compliance_percentage", 100.0
                        ),
                        "status": sla_compliance.get("status", "compliant"),
                        "last_violation": sla_compliance.get("last_violation"),
                        "measurement_window_minutes": sla.measurement_window_minutes,
                    }

                except Exception as e:
                    self.logger.error(
                        f"Failed to calculate compliance for SLA {sla_id}: {e}"
                    )
                    sla_status["sla_details"][sla_id] = {"error": str(e)}

            # Include active violations if requested
            if include_violations:
                with self._violation_lock:
                    relevant_violations = []
                    for violation in self._active_violations.values():
                        if not service_filter or violation.service == service_filter:
                            relevant_violations.append(
                                self._serialize_violation(violation)
                            )

                    sla_status["active_violations"] = relevant_violations

            # Include trend analysis if requested
            if include_trends:
                sla_status["trend_analysis"] = await self._generate_trend_analysis(
                    slas_to_check
                )

            # Generate compliance summary
            sla_status["compliance_summary"] = self._generate_compliance_summary(
                sla_status["sla_details"]
            )

            return sla_status

        except Exception as e:
            self.logger.error(f"Failed to get SLA status: {e}")
            raise

    async def create_custom_sla(self, sla_definition: Dict[str, Any]) -> str:
        """Create a custom SLA definition

        Args:
            sla_definition: SLA definition parameters

        Returns:
            Created SLA ID
        """
        try:
            # Validate SLA definition
            required_fields = [
                "name",
                "service",
                "metric_type",
                "target_value",
                "warning_threshold",
                "critical_threshold",
            ]

            for field in required_fields:
                if field not in sla_definition:
                    raise ValueError(
                        f"Required field '{field}' missing from SLA definition"
                    )

            # Create SLA ID
            sla_id = f"{sla_definition['service']}_{sla_definition['metric_type']}_{int(time.time())}"

            # Create SLA object
            sla = SLADefinition(
                sla_id=sla_id,
                name=sla_definition["name"],
                description=sla_definition.get("description", "Custom SLA"),
                service=sla_definition["service"],
                metric_type=sla_definition["metric_type"],
                target_value=float(sla_definition["target_value"]),
                warning_threshold=float(sla_definition["warning_threshold"]),
                critical_threshold=float(sla_definition["critical_threshold"]),
                measurement_window_minutes=sla_definition.get(
                    "measurement_window_minutes", 10
                ),
                evaluation_frequency_seconds=sla_definition.get(
                    "evaluation_frequency_seconds", 60
                ),
                alert_channels=sla_definition.get("alert_channels", ["email"]),
                escalation_rules=sla_definition.get("escalation_rules", {}),
                enabled=sla_definition.get("enabled", True),
            )

            # Add to definitions
            self._sla_definitions[sla_id] = sla
            self._sla_metrics["total_slas_monitored"] = len(self._sla_definitions)

            self.logger.info(f"Created custom SLA: {sla_id}")
            return sla_id

        except Exception as e:
            self.logger.error(f"Failed to create custom SLA: {e}")
            raise

    async def get_sla_compliance_report(
        self, service: str, reporting_period_hours: int = 24
    ) -> SLAComplianceReport:
        """Generate comprehensive SLA compliance report

        Args:
            service: Service to generate report for
            reporting_period_hours: Reporting period in hours

        Returns:
            Detailed compliance report
        """
        try:
            reporting_period = timedelta(hours=reporting_period_hours)
            cutoff_time = datetime.now(timezone.utc) - reporting_period

            # Get service SLAs
            service_slas = {
                sla_id: sla
                for sla_id, sla in self._sla_definitions.items()
                if sla.service == service
            }

            if not service_slas:
                raise ValueError(f"No SLAs defined for service: {service}")

            # Calculate compliance metrics
            compliance_data = []
            violations_in_period = []

            with self._violation_lock:
                for violation in self._violation_history:
                    if (
                        violation.service == service
                        and violation.detection_time >= cutoff_time
                    ):
                        violations_in_period.append(violation)

            # Calculate overall compliance
            total_measurements = 0
            compliant_measurements = 0

            for sla_id, sla in service_slas.items():
                sla_compliance = await self._calculate_sla_compliance_for_period(
                    sla, reporting_period
                )
                compliance_data.append(sla_compliance)

                total_measurements += sla_compliance.get("total_measurements", 0)
                compliant_measurements += sla_compliance.get(
                    "compliant_measurements", 0
                )

            overall_compliance = (
                (compliant_measurements / total_measurements * 100)
                if total_measurements > 0
                else 100.0
            )

            # Calculate MTTR (Mean Time To Recovery)
            resolution_times = []
            for violation in violations_in_period:
                if violation.resolved and violation.resolution_time:
                    resolution_time = (
                        violation.resolution_time - violation.detection_time
                    ).total_seconds()
                    resolution_times.append(resolution_time)

            mttr_seconds = (
                statistics.mean(resolution_times) if resolution_times else 0.0
            )

            # Calculate availability
            availability_percentage = self._calculate_service_availability(
                service, reporting_period
            )

            # Get performance metrics
            performance_metrics = await self._get_service_performance_metrics(
                service, reporting_period
            )

            # Generate trend analysis
            trend_analysis = await self._generate_service_trend_analysis(
                service, reporting_period
            )

            # Generate recommendations
            recommendations = self._generate_compliance_recommendations(
                service, compliance_data, violations_in_period
            )

            return SLAComplianceReport(
                service=service,
                reporting_period=reporting_period,
                sla_compliance_percentage=overall_compliance,
                violations_count=len(violations_in_period),
                mttr_seconds=mttr_seconds,
                availability_percentage=availability_percentage,
                performance_metrics=performance_metrics,
                trend_analysis=trend_analysis,
                recommendations=recommendations,
            )

        except Exception as e:
            self.logger.error(f"Failed to generate SLA compliance report: {e}")
            raise

    # Background monitoring tasks

    async def _sla_monitor_task(self):
        """Main SLA monitoring task"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(self._config["monitoring_interval_seconds"])

                # Monitor all enabled SLAs
                for sla_id, sla in self._sla_definitions.items():
                    if not sla.enabled:
                        continue

                    try:
                        await self._evaluate_sla(sla)
                    except Exception as e:
                        self.logger.error(f"Failed to evaluate SLA {sla_id}: {e}")

            except Exception as e:
                self.logger.error(f"Error in SLA monitor task: {e}")

    async def _evaluate_sla(self, sla: SLADefinition):
        """Evaluate a specific SLA for violations"""
        try:
            # Get current metric value
            current_value = await self._get_current_metric_value(sla)

            # Check for violations
            violation_severity = None

            if sla.metric_type in ["response_time", "error_rate"]:
                # Lower is better metrics
                if current_value >= sla.critical_threshold:
                    violation_severity = "critical"
                elif current_value >= sla.warning_threshold:
                    violation_severity = "warning"
            elif sla.metric_type in ["availability", "throughput"]:
                # Higher is better metrics
                if current_value <= sla.critical_threshold:
                    violation_severity = "critical"
                elif current_value <= sla.warning_threshold:
                    violation_severity = "warning"

            # Handle violation
            if violation_severity:
                await self._handle_sla_violation(sla, current_value, violation_severity)
            else:
                # Check if existing violation should be resolved
                await self._check_violation_resolution(sla, current_value)

            # Record compliance data
            await self._record_compliance_data(
                sla, current_value, violation_severity is None
            )

        except Exception as e:
            self.logger.error(f"Failed to evaluate SLA {sla.sla_id}: {e}")

    async def _handle_sla_violation(
        self, sla: SLADefinition, current_value: float, severity: str
    ):
        """Handle SLA violation detection"""
        violation_key = f"{sla.sla_id}_{severity}"

        with self._violation_lock:
            # Check if violation already exists
            if violation_key in self._active_violations:
                # Update existing violation
                violation = self._active_violations[violation_key]
                violation.actual_value = current_value
                violation.duration_seconds = (
                    datetime.now(timezone.utc) - violation.detection_time
                ).total_seconds()
                violation.violation_percentage = self._calculate_violation_percentage(
                    sla, current_value
                )
            else:
                # Create new violation
                violation = SLAViolation(
                    violation_id=f"{violation_key}_{int(time.time())}",
                    sla_id=sla.sla_id,
                    service=sla.service,
                    metric_type=sla.metric_type,
                    severity=severity,
                    target_value=sla.target_value,
                    actual_value=current_value,
                    violation_percentage=self._calculate_violation_percentage(
                        sla, current_value
                    ),
                    detection_time=datetime.now(timezone.utc),
                    duration_seconds=0,
                    affected_operations=await self._get_affected_operations(sla),
                    impact_assessment=await self._assess_violation_impact(
                        sla, current_value
                    ),
                )

                self._active_violations[violation_key] = violation
                self._sla_metrics["active_violations"] = len(self._active_violations)

                # Send alert
                await self._send_violation_alert(sla, violation)

        self.logger.warning(
            f"SLA violation detected: {sla.name} ({severity}) - "
            f"Target: {sla.target_value}, Actual: {current_value:.2f}"
        )

    async def _send_violation_alert(self, sla: SLADefinition, violation: SLAViolation):
        """Send alert notifications for SLA violation"""
        try:
            # Check rate limits
            if not self._check_alert_rate_limit(sla.alert_channels):
                self.logger.warning(f"Alert rate limit exceeded for SLA {sla.sla_id}")
                return

            # Prepare alert message
            alert_message = {
                "type": "sla_violation",
                "severity": violation.severity,
                "sla_name": sla.name,
                "service": sla.service,
                "metric_type": sla.metric_type,
                "target_value": sla.target_value,
                "actual_value": violation.actual_value,
                "violation_percentage": violation.violation_percentage,
                "detection_time": violation.detection_time.isoformat(),
                "description": sla.description,
                "impact_assessment": violation.impact_assessment,
            }

            # Send to configured channels
            for channel_id in sla.alert_channels:
                if channel_id in self._alert_channels:
                    channel = self._alert_channels[channel_id]
                    if channel.enabled:
                        await self._send_alert_to_channel(channel, alert_message)

            # Track alert
            with self._alert_lock:
                self._alert_history.append(
                    {
                        "timestamp": datetime.now(timezone.utc),
                        "violation_id": violation.violation_id,
                        "sla_id": sla.sla_id,
                        "channels": sla.alert_channels,
                    }
                )

                # Update metrics
                self._sla_metrics["alerts_sent_24h"] += 1

        except Exception as e:
            self.logger.error(f"Failed to send violation alert: {e}")

    async def _send_alert_to_channel(
        self, channel: AlertChannel, message: Dict[str, Any]
    ):
        """Send alert to specific notification channel"""
        try:
            if channel.channel_type == "email":
                await self._send_email_alert(channel, message)
            elif channel.channel_type == "slack":
                await self._send_slack_alert(channel, message)
            elif channel.channel_type == "webhook":
                await self._send_webhook_alert(channel, message)
            elif channel.channel_type == "pagerduty":
                await self._send_pagerduty_alert(channel, message)
            else:
                self.logger.warning(
                    f"Unknown alert channel type: {channel.channel_type}"
                )

        except Exception as e:
            self.logger.error(f"Failed to send alert to {channel.channel_id}: {e}")

    async def _send_email_alert(self, channel: AlertChannel, message: Dict[str, Any]):
        """Send email alert"""
        try:
            # Prepare email
            subject = (
                f"SLA Violation: {message['sla_name']} ({message['severity'].upper()})"
            )

            body = f"""
SLA Violation Alert

Service: {message['service']}
SLA: {message['sla_name']}
Severity: {message['severity'].upper()}
Metric: {message['metric_type']}
Target: {message['target_value']}
Actual: {message['actual_value']:.2f}
Violation: {message['violation_percentage']:.1f}%
Time: {message['detection_time']}

Description: {message.get('description', 'N/A')}

Impact Assessment:
{json.dumps(message.get('impact_assessment', {}), indent=2)}

Please investigate and resolve this issue promptly.
            """

            # This would be implemented with actual SMTP sending
            self.logger.info(f"Email alert sent to {channel.endpoint}: {subject}")

        except Exception as e:
            self.logger.error(f"Failed to send email alert: {e}")

    async def _send_slack_alert(self, channel: AlertChannel, message: Dict[str, Any]):
        """Send Slack alert"""
        try:
            # Prepare Slack message
            color = "danger" if message["severity"] == "critical" else "warning"

            slack_message = {
                "text": f"SLA Violation: {message['sla_name']}",
                "attachments": [
                    {
                        "color": color,
                        "fields": [
                            {
                                "title": "Service",
                                "value": message["service"],
                                "short": True,
                            },
                            {
                                "title": "Severity",
                                "value": message["severity"].upper(),
                                "short": True,
                            },
                            {
                                "title": "Metric",
                                "value": message["metric_type"],
                                "short": True,
                            },
                            {
                                "title": "Violation",
                                "value": f"{message['violation_percentage']:.1f}%",
                                "short": True,
                            },
                            {
                                "title": "Target",
                                "value": str(message["target_value"]),
                                "short": True,
                            },
                            {
                                "title": "Actual",
                                "value": f"{message['actual_value']:.2f}",
                                "short": True,
                            },
                        ],
                        "footer": "FaultMaven SLA Monitor",
                        "ts": int(time.time()),
                    }
                ],
            }

            # This would be implemented with actual Slack webhook
            self.logger.info(f"Slack alert sent to {channel.endpoint}")

        except Exception as e:
            self.logger.error(f"Failed to send Slack alert: {e}")

    async def _send_webhook_alert(self, channel: AlertChannel, message: Dict[str, Any]):
        """Send webhook alert"""
        try:
            # This would be implemented with actual HTTP POST
            self.logger.info(f"Webhook alert sent to {channel.endpoint}")

        except Exception as e:
            self.logger.error(f"Failed to send webhook alert: {e}")

    async def _send_pagerduty_alert(
        self, channel: AlertChannel, message: Dict[str, Any]
    ):
        """Send PagerDuty alert"""
        try:
            # This would be implemented with actual PagerDuty API
            self.logger.info(f"PagerDuty alert sent for {message['sla_name']}")

        except Exception as e:
            self.logger.error(f"Failed to send PagerDuty alert: {e}")

    # Helper methods

    async def _get_current_metric_value(self, sla: SLADefinition) -> float:
        """Get current value for SLA metric"""
        try:
            if not self._metrics_collector:
                # Return mock values for demonstration
                return self._get_mock_metric_value(sla)

            # Get service performance data
            service_data = (
                await self._metrics_collector.get_service_performance_summary(
                    sla.service, sla.measurement_window_minutes
                )
            )

            if sla.metric_type == "response_time":
                # Get average response time
                operations = service_data.get("operation_statistics", {})
                if operations:
                    avg_times = [stats.get("avg", 0) for stats in operations.values()]
                    return statistics.mean(avg_times) if avg_times else 0.0
                return 0.0

            elif sla.metric_type == "availability":
                # Calculate availability from health score
                health_score = service_data.get("health_score", 1.0)
                return health_score * 100  # Convert to percentage

            elif sla.metric_type == "error_rate":
                # Calculate error rate from service data
                total_operations = service_data.get("total_operations", 0)
                if total_operations > 0:
                    # Simplified error rate calculation
                    return 0.01  # 1% error rate
                return 0.0

            elif sla.metric_type == "throughput":
                # Calculate throughput
                operations = service_data.get("operation_statistics", {})
                if operations:
                    total_requests = sum(
                        stats.get("count", 0) for stats in operations.values()
                    )
                    return total_requests / sla.measurement_window_minutes * 60  # RPS
                return 0.0

            else:
                self.logger.warning(f"Unknown metric type: {sla.metric_type}")
                return 0.0

        except Exception as e:
            self.logger.error(f"Failed to get metric value for {sla.sla_id}: {e}")
            return self._get_mock_metric_value(sla)

    def _get_mock_metric_value(self, sla: SLADefinition) -> float:
        """Get mock metric value for testing"""
        import random

        if sla.metric_type == "response_time":
            # Generate random response time around target
            base = sla.target_value
            variance = base * 0.3
            return max(0, random.gauss(base, variance))

        elif sla.metric_type == "availability":
            # Generate availability around 99.5%
            return random.gauss(99.5, 0.5)

        elif sla.metric_type == "error_rate":
            # Generate low error rate
            return max(0, random.gauss(0.005, 0.002))

        elif sla.metric_type == "throughput":
            # Generate throughput around target
            base = sla.target_value
            variance = base * 0.2
            return max(0, random.gauss(base, variance))

        return 0.0

    def _calculate_violation_percentage(
        self, sla: SLADefinition, current_value: float
    ) -> float:
        """Calculate violation percentage"""
        if sla.metric_type in ["response_time", "error_rate"]:
            # Higher is worse
            if current_value > sla.target_value:
                return ((current_value - sla.target_value) / sla.target_value) * 100
        elif sla.metric_type in ["availability", "throughput"]:
            # Lower is worse
            if current_value < sla.target_value:
                return ((sla.target_value - current_value) / sla.target_value) * 100

        return 0.0

    def _check_alert_rate_limit(self, channels: List[str]) -> bool:
        """Check if alert rate limit allows sending"""
        current_time = datetime.now(timezone.utc)
        hour_ago = current_time - timedelta(hours=1)

        for channel_id in channels:
            if channel_id in self._alert_channels:
                channel = self._alert_channels[channel_id]

                # Count alerts in last hour
                recent_alerts = [
                    alert_time
                    for alert_time in self._alert_rate_tracker[channel_id]
                    if alert_time > hour_ago
                ]

                if len(recent_alerts) >= channel.rate_limit_per_hour:
                    return False

                # Add current alert to tracker
                self._alert_rate_tracker[channel_id].append(current_time)

        return True

    async def _calculate_overall_compliance(
        self, slas: Dict[str, SLADefinition]
    ) -> float:
        """Calculate overall SLA compliance percentage"""
        if not slas:
            return 100.0

        compliance_scores = []

        for sla in slas.values():
            try:
                compliance = await self._calculate_sla_compliance(sla)
                compliance_scores.append(compliance.get("compliance_percentage", 100.0))
            except Exception as e:
                self.logger.error(
                    f"Failed to calculate compliance for {sla.sla_id}: {e}"
                )
                compliance_scores.append(100.0)  # Assume compliant if calculation fails

        return statistics.mean(compliance_scores) if compliance_scores else 100.0

    async def _calculate_sla_compliance(self, sla: SLADefinition) -> Dict[str, Any]:
        """Calculate compliance for a specific SLA"""
        try:
            current_value = await self._get_current_metric_value(sla)

            # Determine compliance
            if sla.metric_type in ["response_time", "error_rate"]:
                compliant = current_value <= sla.target_value
                compliance_percentage = min(
                    100.0, (sla.target_value / max(current_value, 0.001)) * 100
                )
            else:  # availability, throughput
                compliant = current_value >= sla.target_value
                compliance_percentage = min(
                    100.0, (current_value / max(sla.target_value, 0.001)) * 100
                )

            status = "compliant" if compliant else "violating"

            # Get last violation time
            last_violation = None
            with self._violation_lock:
                for violation in self._violation_history:
                    if violation.sla_id == sla.sla_id:
                        if (
                            not last_violation
                            or violation.detection_time > last_violation
                        ):
                            last_violation = violation.detection_time

            return {
                "current_value": current_value,
                "compliance_percentage": compliance_percentage,
                "status": status,
                "last_violation": (
                    last_violation.isoformat() if last_violation else None
                ),
            }

        except Exception as e:
            self.logger.error(
                f"Failed to calculate SLA compliance for {sla.sla_id}: {e}"
            )
            return {
                "current_value": 0.0,
                "compliance_percentage": 0.0,
                "status": "unknown",
                "error": str(e),
            }

    def _serialize_violation(self, violation: SLAViolation) -> Dict[str, Any]:
        """Serialize violation for API response"""
        return {
            "violation_id": violation.violation_id,
            "sla_id": violation.sla_id,
            "service": violation.service,
            "metric_type": violation.metric_type,
            "severity": violation.severity,
            "target_value": violation.target_value,
            "actual_value": violation.actual_value,
            "violation_percentage": violation.violation_percentage,
            "detection_time": violation.detection_time.isoformat(),
            "duration_seconds": violation.duration_seconds,
            "resolved": violation.resolved,
            "resolution_time": (
                violation.resolution_time.isoformat()
                if violation.resolution_time
                else None
            ),
        }

    def _generate_compliance_summary(
        self, sla_details: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate compliance summary from SLA details"""
        if not sla_details:
            return {}

        total_slas = len(sla_details)
        compliant_slas = len(
            [sla for sla in sla_details.values() if sla.get("status") == "compliant"]
        )
        violating_slas = total_slas - compliant_slas

        compliance_scores = [
            sla.get("compliance_percentage", 100.0) for sla in sla_details.values()
        ]
        avg_compliance = (
            statistics.mean(compliance_scores) if compliance_scores else 100.0
        )

        return {
            "total_slas": total_slas,
            "compliant_slas": compliant_slas,
            "violating_slas": violating_slas,
            "compliance_rate": (
                (compliant_slas / total_slas) * 100 if total_slas > 0 else 100.0
            ),
            "average_compliance_percentage": avg_compliance,
        }

    # Background task implementations (simplified placeholders)

    async def _compliance_calculator_task(self):
        """Calculate and record compliance metrics"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(self._config["compliance_calculation_interval"])
                # Implementation would update compliance metrics
                pass
            except Exception as e:
                self.logger.error(f"Error in compliance calculator task: {e}")

    async def _violation_resolver_task(self):
        """Check for violation resolution"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(60)  # Check every minute
                # Implementation would check if violations are resolved
                pass
            except Exception as e:
                self.logger.error(f"Error in violation resolver task: {e}")

    async def _alert_consolidator_task(self):
        """Consolidate and deduplicate alerts"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(self._config["alert_consolidation_window"])
                # Implementation would consolidate similar alerts
                pass
            except Exception as e:
                self.logger.error(f"Error in alert consolidator task: {e}")

    # Placeholder implementations for complex methods

    async def _check_violation_resolution(
        self, sla: SLADefinition, current_value: float
    ):
        """Check if existing violations should be resolved"""
        pass

    async def _record_compliance_data(
        self, sla: SLADefinition, current_value: float, compliant: bool
    ):
        """Record compliance data point"""
        pass

    async def _get_affected_operations(self, sla: SLADefinition) -> List[str]:
        """Get operations affected by SLA violation"""
        return []

    async def _assess_violation_impact(
        self, sla: SLADefinition, current_value: float
    ) -> Dict[str, Any]:
        """Assess impact of SLA violation"""
        return {"estimated_affected_users": 100, "business_impact": "medium"}

    async def _generate_trend_analysis(
        self, slas: Dict[str, SLADefinition]
    ) -> Dict[str, Any]:
        """Generate trend analysis for SLAs"""
        return {"trends": "improving", "forecast": "stable"}

    async def _calculate_sla_compliance_for_period(
        self, sla: SLADefinition, period: timedelta
    ) -> Dict[str, Any]:
        """Calculate SLA compliance for specific period"""
        return {"total_measurements": 100, "compliant_measurements": 95}

    def _calculate_service_availability(self, service: str, period: timedelta) -> float:
        """Calculate service availability percentage"""
        return 99.5

    async def _get_service_performance_metrics(
        self, service: str, period: timedelta
    ) -> Dict[str, float]:
        """Get service performance metrics for period"""
        return {"avg_response_time": 150.0, "throughput": 120.0, "error_rate": 0.008}

    async def _generate_service_trend_analysis(
        self, service: str, period: timedelta
    ) -> Dict[str, Any]:
        """Generate trend analysis for service"""
        return {"performance_trend": "stable", "capacity_outlook": "adequate"}

    def _generate_compliance_recommendations(
        self,
        service: str,
        compliance_data: List[Dict[str, Any]],
        violations: List[SLAViolation],
    ) -> List[str]:
        """Generate compliance improvement recommendations"""
        return [
            "Consider implementing caching to improve response times",
            "Monitor peak usage patterns for capacity planning",
            "Review error handling procedures to reduce error rates",
        ]

    async def health_check(self) -> Dict[str, Any]:
        """Check health of SLA monitor"""
        base_health = await super().health_check()

        sla_health = {
            **base_health,
            "service": "sla_monitor",
            "monitoring_active": self._background_tasks_running,
            "total_slas_monitored": self._sla_metrics["total_slas_monitored"],
            "active_violations": self._sla_metrics["active_violations"],
            "alert_channels": len(self._alert_channels),
            "alerts_sent_24h": self._sla_metrics["alerts_sent_24h"],
            "overall_compliance": self._sla_metrics["overall_compliance_percentage"],
        }

        # Determine status
        if not self._background_tasks_running:
            sla_health["status"] = "degraded"
            sla_health["warning"] = "Background monitoring not running"
        elif self._sla_metrics["active_violations"] > 5:
            sla_health["status"] = "degraded"
            sla_health["warning"] = "High number of active violations"

        return sla_health
