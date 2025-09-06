"""
Alerting Framework

Provides configurable alerting capabilities for performance monitoring
with multiple alert channels and intelligent alert management.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable, Union
from datetime import datetime, timedelta
from enum import Enum
import logging
import asyncio
import json


class AlertSeverity(Enum):
    """Alert severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertStatus(Enum):
    """Alert status."""
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


class AlertChannel(Enum):
    """Alert notification channels."""
    LOG = "log"
    EMAIL = "email"
    SLACK = "slack"
    WEBHOOK = "webhook"
    SMS = "sms"


@dataclass
class AlertRule:
    """Defines an alert rule with conditions and actions."""
    rule_id: str
    name: str
    description: str
    metric_name: str
    condition: str  # "greater_than", "less_than", "equals", "not_equals"
    threshold_value: float
    severity: AlertSeverity
    enabled: bool = True
    evaluation_window_minutes: int = 5
    min_occurrences: int = 1
    max_occurrences_per_hour: int = 10
    channels: List[AlertChannel] = field(default_factory=list)
    tags: Dict[str, str] = field(default_factory=dict)
    suppress_duration_minutes: int = 60
    auto_resolve: bool = True
    custom_message: Optional[str] = None


@dataclass
class Alert:
    """Represents an active or resolved alert."""
    alert_id: str
    rule_id: str
    rule_name: str
    metric_name: str
    metric_value: float
    threshold_value: float
    severity: AlertSeverity
    status: AlertStatus
    triggered_at: datetime
    resolved_at: Optional[datetime] = None
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    notification_count: int = 0
    last_notification: Optional[datetime] = None


@dataclass
class AlertChannelConfig:
    """Configuration for alert notification channels."""
    channel: AlertChannel
    enabled: bool = True
    config: Dict[str, Any] = field(default_factory=dict)


class AlertManager:
    """Manages alert rules, evaluation, and notification."""
    
    def __init__(self, settings=None):
        """Initialize alert manager with unified settings."""
        self.logger = logging.getLogger(__name__)
        
        # Get settings using unified configuration system
        if settings is None:
            from faultmaven.config.settings import get_settings
            settings = get_settings()
        self.settings = settings
        
        self.alert_rules: Dict[str, AlertRule] = {}
        self.active_alerts: Dict[str, Alert] = {}
        self.alert_history: List[Alert] = []
        self.channel_configs: Dict[AlertChannel, AlertChannelConfig] = {}
        self.notification_handlers: Dict[AlertChannel, Callable] = {}
        self.suppressed_rules: Dict[str, datetime] = {}
        self.is_running = False
        self.evaluation_task: Optional[asyncio.Task] = None
        
        self._initialize_default_channels()
        self._register_default_handlers()
    
    def _initialize_default_channels(self) -> None:
        """Initialize default alert channels."""
        # Log channel (always available)
        self.channel_configs[AlertChannel.LOG] = AlertChannelConfig(
            channel=AlertChannel.LOG,
            enabled=True,
            config={"log_level": "WARNING"}
        )
        
        # Webhook channel (using unified settings)
        webhook_url = self.settings.alerting.alert_webhook_url
        self.channel_configs[AlertChannel.WEBHOOK] = AlertChannelConfig(
            channel=AlertChannel.WEBHOOK,
            enabled=webhook_url is not None,
            config={
                "url": webhook_url,
                "timeout_seconds": 30,
                "retry_attempts": 3
            }
        )
        
        # Email channel (using unified settings)
        to_emails = self.settings.alerting.alert_to_emails.split(",") if self.settings.alerting.alert_to_emails else []
        self.channel_configs[AlertChannel.EMAIL] = AlertChannelConfig(
            channel=AlertChannel.EMAIL,
            enabled=bool(self.settings.alerting.alert_from_email and to_emails),
            config={
                "smtp_host": self.settings.alerting.smtp_host,
                "smtp_port": self.settings.alerting.smtp_port,
                "from_email": self.settings.alerting.alert_from_email,
                "to_emails": to_emails
            }
        )
    
    def _register_default_handlers(self) -> None:
        """Register default notification handlers."""
        self.notification_handlers[AlertChannel.LOG] = self._send_log_notification
        self.notification_handlers[AlertChannel.WEBHOOK] = self._send_webhook_notification
        self.notification_handlers[AlertChannel.EMAIL] = self._send_email_notification
    
    def add_alert_rule(self, rule: AlertRule) -> None:
        """Add an alert rule.
        
        Args:
            rule: Alert rule to add
        """
        self.alert_rules[rule.rule_id] = rule
        self.logger.info(f"Added alert rule: {rule.name} ({rule.rule_id})")
    
    def remove_alert_rule(self, rule_id: str) -> bool:
        """Remove an alert rule.
        
        Args:
            rule_id: ID of the rule to remove
            
        Returns:
            True if rule was removed, False if not found
        """
        if rule_id in self.alert_rules:
            del self.alert_rules[rule_id]
            self.logger.info(f"Removed alert rule: {rule_id}")
            return True
        return False
    
    def enable_rule(self, rule_id: str) -> bool:
        """Enable an alert rule.
        
        Args:
            rule_id: ID of the rule to enable
            
        Returns:
            True if rule was enabled, False if not found
        """
        if rule_id in self.alert_rules:
            self.alert_rules[rule_id].enabled = True
            self.logger.info(f"Enabled alert rule: {rule_id}")
            return True
        return False
    
    def disable_rule(self, rule_id: str) -> bool:
        """Disable an alert rule.
        
        Args:
            rule_id: ID of the rule to disable
            
        Returns:
            True if rule was disabled, False if not found
        """
        if rule_id in self.alert_rules:
            self.alert_rules[rule_id].enabled = False
            self.logger.info(f"Disabled alert rule: {rule_id}")
            return True
        return False
    
    def suppress_rule(self, rule_id: str, duration_minutes: int = 60) -> bool:
        """Suppress an alert rule for a specified duration.
        
        Args:
            rule_id: ID of the rule to suppress
            duration_minutes: How long to suppress the rule
            
        Returns:
            True if rule was suppressed, False if not found
        """
        if rule_id in self.alert_rules:
            suppress_until = datetime.utcnow() + timedelta(minutes=duration_minutes)
            self.suppressed_rules[rule_id] = suppress_until
            self.logger.info(f"Suppressed alert rule {rule_id} until {suppress_until}")
            return True
        return False
    
    def evaluate_metric(self, metric_name: str, metric_value: float, metadata: Optional[Dict[str, Any]] = None) -> List[Alert]:
        """Evaluate a metric against all applicable alert rules.
        
        Args:
            metric_name: Name of the metric
            metric_value: Current value of the metric
            metadata: Additional metadata about the metric
            
        Returns:
            List of triggered alerts
        """
        triggered_alerts = []
        current_time = datetime.utcnow()
        
        for rule in self.alert_rules.values():
            if not rule.enabled or rule.metric_name != metric_name:
                continue
            
            # Check if rule is suppressed
            if rule.rule_id in self.suppressed_rules:
                if current_time < self.suppressed_rules[rule.rule_id]:
                    continue  # Still suppressed
                else:
                    del self.suppressed_rules[rule.rule_id]  # Remove expired suppression
            
            # Evaluate rule condition
            if self._evaluate_condition(rule, metric_value):
                # Check if we should trigger an alert
                alert = self._handle_rule_violation(rule, metric_value, metadata or {})
                if alert:
                    triggered_alerts.append(alert)
            else:
                # Check if we should resolve an existing alert
                self._handle_rule_resolution(rule)
        
        return triggered_alerts
    
    def _evaluate_condition(self, rule: AlertRule, metric_value: float) -> bool:
        """Evaluate if a rule condition is met.
        
        Args:
            rule: Alert rule to evaluate
            metric_value: Current metric value
            
        Returns:
            True if condition is met
        """
        if rule.condition == "greater_than":
            return metric_value > rule.threshold_value
        elif rule.condition == "less_than":
            return metric_value < rule.threshold_value
        elif rule.condition == "equals":
            return abs(metric_value - rule.threshold_value) < 0.001  # Float comparison
        elif rule.condition == "not_equals":
            return abs(metric_value - rule.threshold_value) >= 0.001
        else:
            self.logger.warning(f"Unknown condition: {rule.condition}")
            return False
    
    def _handle_rule_violation(self, rule: AlertRule, metric_value: float, metadata: Dict[str, Any]) -> Optional[Alert]:
        """Handle a rule violation by creating or updating an alert.
        
        Args:
            rule: Violated alert rule
            metric_value: Current metric value
            metadata: Additional metadata
            
        Returns:
            Alert if one was triggered, None otherwise
        """
        current_time = datetime.utcnow()
        
        # Check if alert already exists for this rule
        existing_alert = None
        for alert in self.active_alerts.values():
            if alert.rule_id == rule.rule_id and alert.status == AlertStatus.ACTIVE:
                existing_alert = alert
                break
        
        if existing_alert:
            # Update existing alert
            existing_alert.metric_value = metric_value
            existing_alert.metadata.update(metadata)
            
            # Check if we should send another notification
            if self._should_send_notification(existing_alert, rule):
                self._schedule_notification(existing_alert, rule)
                existing_alert.notification_count += 1
                existing_alert.last_notification = current_time
            
            return None  # Don't return existing alert as "new"
        
        else:
            # Create new alert
            alert_id = f"{rule.rule_id}_{int(current_time.timestamp())}"
            
            message = rule.custom_message or f"{rule.name}: {rule.metric_name} = {metric_value} (threshold: {rule.threshold_value})"
            
            alert = Alert(
                alert_id=alert_id,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                metric_name=rule.metric_name,
                metric_value=metric_value,
                threshold_value=rule.threshold_value,
                severity=rule.severity,
                status=AlertStatus.ACTIVE,
                triggered_at=current_time,
                message=message,
                metadata=metadata,
                notification_count=1,
                last_notification=current_time
            )
            
            self.active_alerts[alert_id] = alert
            self.alert_history.append(alert)
            
            # Send notifications
            self._schedule_notification(alert, rule)
            
            self.logger.warning(f"Alert triggered: {alert.message}")
            return alert
    
    def _handle_rule_resolution(self, rule: AlertRule) -> None:
        """Handle rule resolution (condition no longer met).
        
        Args:
            rule: Alert rule that is no longer violated
        """
        if not rule.auto_resolve:
            return
        
        # Find active alerts for this rule
        alerts_to_resolve = []
        for alert in self.active_alerts.values():
            if alert.rule_id == rule.rule_id and alert.status == AlertStatus.ACTIVE:
                alerts_to_resolve.append(alert)
        
        current_time = datetime.utcnow()
        
        for alert in alerts_to_resolve:
            alert.status = AlertStatus.RESOLVED
            alert.resolved_at = current_time
            
            # Send resolution notification
            self._schedule_resolution_notification(alert, rule)
            
            self.logger.info(f"Alert resolved: {alert.rule_name}")
    
    def _should_send_notification(self, alert: Alert, rule: AlertRule) -> bool:
        """Determine if a notification should be sent for an alert.
        
        Args:
            alert: The alert
            rule: The alert rule
            
        Returns:
            True if notification should be sent
        """
        current_time = datetime.utcnow()
        
        # Check max occurrences per hour
        if alert.notification_count >= rule.max_occurrences_per_hour:
            time_diff = current_time - alert.triggered_at
            if time_diff.total_seconds() < 3600:  # Less than 1 hour
                return False
        
        # Check minimum time between notifications (suppress duration)
        if alert.last_notification:
            time_since_last = current_time - alert.last_notification
            if time_since_last.total_seconds() < rule.suppress_duration_minutes * 60:
                return False
        
        return True
    
    def _schedule_notification(self, alert: Alert, rule: AlertRule) -> None:
        """Schedule alert notification, handling async context properly.
        
        Args:
            alert: Alert to send notifications for
            rule: Associated alert rule
        """
        try:
            # Try to create task if we're in an async context
            asyncio.create_task(self._send_alert_notifications(alert, rule))
        except RuntimeError:
            # No running event loop, use synchronous notification
            self._send_sync_notification(alert, rule, "triggered")
    
    def _schedule_resolution_notification(self, alert: Alert, rule: AlertRule) -> None:
        """Schedule resolution notification, handling async context properly.
        
        Args:
            alert: Resolved alert
            rule: Associated alert rule
        """
        try:
            # Try to create task if we're in an async context
            asyncio.create_task(self._send_resolution_notifications(alert, rule))
        except RuntimeError:
            # No running event loop, use synchronous notification
            self._send_sync_notification(alert, rule, "resolved")
    
    def _send_sync_notification(self, alert: Alert, rule: AlertRule, action: str) -> None:
        """Send notifications synchronously for non-async contexts.
        
        Args:
            alert: Alert information
            rule: Associated alert rule
            action: "triggered" or "resolved"
        """
        for channel in rule.channels:
            if channel in self.channel_configs and self.channel_configs[channel].enabled:
                if channel == AlertChannel.LOG:
                    # Log notifications can be sent synchronously
                    try:
                        import asyncio
                        asyncio.run(self._send_log_notification(alert, rule, action))
                    except Exception as e:
                        self.logger.error(f"Failed to send log notification: {e}")
                else:
                    # For other channels, just log that they would be sent
                    self.logger.info(f"Would send {action} notification to {channel.value} for alert: {alert.message}")
    
    async def _send_alert_notifications(self, alert: Alert, rule: AlertRule) -> None:
        """Send notifications for an alert.
        
        Args:
            alert: Alert to send notifications for
            rule: Associated alert rule
        """
        for channel in rule.channels:
            if channel in self.channel_configs and self.channel_configs[channel].enabled:
                if channel in self.notification_handlers:
                    try:
                        await self.notification_handlers[channel](alert, rule, "triggered")
                    except Exception as e:
                        self.logger.error(f"Failed to send alert notification to {channel.value}: {e}")
    
    async def _send_resolution_notifications(self, alert: Alert, rule: AlertRule) -> None:
        """Send resolution notifications for an alert.
        
        Args:
            alert: Resolved alert
            rule: Associated alert rule
        """
        for channel in rule.channels:
            if channel in self.channel_configs and self.channel_configs[channel].enabled:
                if channel in self.notification_handlers:
                    try:
                        await self.notification_handlers[channel](alert, rule, "resolved")
                    except Exception as e:
                        self.logger.error(f"Failed to send resolution notification to {channel.value}: {e}")
    
    async def _send_log_notification(self, alert: Alert, rule: AlertRule, action: str) -> None:
        """Send log notification.
        
        Args:
            alert: Alert information
            rule: Alert rule
            action: "triggered" or "resolved"
        """
        log_level = self.channel_configs[AlertChannel.LOG].config.get("log_level", "WARNING")
        
        if action == "triggered":
            message = f"ALERT TRIGGERED: {alert.message}"
        else:
            message = f"ALERT RESOLVED: {alert.rule_name}"
        
        if log_level == "CRITICAL":
            self.logger.critical(message)
        elif log_level == "ERROR":
            self.logger.error(message)
        elif log_level == "WARNING":
            self.logger.warning(message)
        else:
            self.logger.info(message)
    
    async def _send_webhook_notification(self, alert: Alert, rule: AlertRule, action: str) -> None:
        """Send webhook notification.
        
        Args:
            alert: Alert information
            rule: Alert rule
            action: "triggered" or "resolved"
        """
        webhook_config = self.channel_configs[AlertChannel.WEBHOOK].config
        webhook_url = webhook_config.get("url")
        
        if not webhook_url:
            return
        
        try:
            import aiohttp
            
            payload = {
                "action": action,
                "alert": {
                    "id": alert.alert_id,
                    "rule_name": alert.rule_name,
                    "metric_name": alert.metric_name,
                    "metric_value": alert.metric_value,
                    "threshold_value": alert.threshold_value,
                    "severity": alert.severity.value,
                    "status": alert.status.value,
                    "triggered_at": alert.triggered_at.isoformat(),
                    "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
                    "message": alert.message,
                    "metadata": alert.metadata
                },
                "rule": {
                    "id": rule.rule_id,
                    "name": rule.name,
                    "description": rule.description,
                    "condition": rule.condition,
                    "threshold": rule.threshold_value
                },
                "timestamp": datetime.utcnow().isoformat()
            }
            
            timeout = aiohttp.ClientTimeout(total=webhook_config.get("timeout_seconds", 30))
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(webhook_url, json=payload) as response:
                    if response.status == 200:
                        self.logger.debug(f"Webhook notification sent successfully for {action}")
                    else:
                        self.logger.warning(f"Webhook notification failed: {response.status}")
        
        except Exception as e:
            self.logger.error(f"Webhook notification error: {e}")
    
    async def _send_email_notification(self, alert: Alert, rule: AlertRule, action: str) -> None:
        """Send email notification.
        
        Args:
            alert: Alert information
            rule: Alert rule
            action: "triggered" or "resolved"
        """
        # Placeholder for email implementation
        self.logger.info(f"Email notification would be sent for {action}: {alert.message}")
    
    def get_active_alerts(self, severity: Optional[AlertSeverity] = None) -> List[Alert]:
        """Get active alerts, optionally filtered by severity.
        
        Args:
            severity: Optional severity filter
            
        Returns:
            List of active alerts
        """
        alerts = [alert for alert in self.active_alerts.values() if alert.status == AlertStatus.ACTIVE]
        
        if severity:
            alerts = [alert for alert in alerts if alert.severity == severity]
        
        return sorted(alerts, key=lambda x: x.triggered_at, reverse=True)
    
    def get_alert_statistics(self) -> Dict[str, Any]:
        """Get alert statistics.
        
        Returns:
            Alert statistics
        """
        active_alerts = self.get_active_alerts()
        
        # Count by severity
        severity_counts = {}
        for severity in AlertSeverity:
            severity_counts[severity.value] = len([a for a in active_alerts if a.severity == severity])
        
        # Recent alerts (last 24 hours)
        recent_threshold = datetime.utcnow() - timedelta(hours=24)
        recent_alerts = [a for a in self.alert_history if a.triggered_at >= recent_threshold]
        
        return {
            "total_rules": len(self.alert_rules),
            "enabled_rules": len([r for r in self.alert_rules.values() if r.enabled]),
            "active_alerts": len(active_alerts),
            "severity_breakdown": severity_counts,
            "alerts_24h": len(recent_alerts),
            "suppressed_rules": len(self.suppressed_rules),
            "configured_channels": list(self.channel_configs.keys()),
            "enabled_channels": [c for c, config in self.channel_configs.items() if config.enabled]
        }
    
    def configure_channel(self, channel: AlertChannel, config: AlertChannelConfig) -> None:
        """Configure an alert notification channel.
        
        Args:
            channel: Alert channel to configure
            config: Channel configuration
        """
        self.channel_configs[channel] = config
        self.logger.info(f"Configured alert channel: {channel.value}")
    
    def add_notification_handler(self, channel: AlertChannel, handler: Callable) -> None:
        """Add custom notification handler for a channel.
        
        Args:
            channel: Alert channel
            handler: Async function to handle notifications
        """
        self.notification_handlers[channel] = handler
        self.logger.info(f"Added notification handler for {channel.value}")


# Global alert manager instance
alert_manager = AlertManager()


# Default alert rules for FaultMaven
def setup_default_alert_rules() -> None:
    """Set up default alert rules for FaultMaven."""
    default_rules = [
        AlertRule(
            rule_id="api_response_time_high",
            name="API Response Time High",
            description="API response time exceeds acceptable threshold",
            metric_name="api.request_duration",
            condition="greater_than",
            threshold_value=500.0,
            severity=AlertSeverity.MEDIUM,
            channels=[AlertChannel.LOG, AlertChannel.WEBHOOK],
            evaluation_window_minutes=5,
            suppress_duration_minutes=30
        ),
        AlertRule(
            rule_id="api_error_rate_high",
            name="API Error Rate High",
            description="API error rate is too high",
            metric_name="api.error_rate",
            condition="greater_than",
            threshold_value=5.0,
            severity=AlertSeverity.HIGH,
            channels=[AlertChannel.LOG, AlertChannel.WEBHOOK],
            evaluation_window_minutes=5,
            suppress_duration_minutes=15
        ),
        AlertRule(
            rule_id="llm_response_time_critical",
            name="LLM Response Time Critical",
            description="LLM response time is critically high",
            metric_name="llm.request_duration",
            condition="greater_than",
            threshold_value=10000.0,
            severity=AlertSeverity.CRITICAL,
            channels=[AlertChannel.LOG, AlertChannel.WEBHOOK],
            evaluation_window_minutes=2,
            suppress_duration_minutes=10
        ),
        AlertRule(
            rule_id="database_response_time_high",
            name="Database Response Time High",
            description="Database query response time is high",
            metric_name="database.query_duration",
            condition="greater_than",
            threshold_value=200.0,
            severity=AlertSeverity.MEDIUM,
            channels=[AlertChannel.LOG],
            evaluation_window_minutes=5,
            suppress_duration_minutes=30
        )
    ]
    
    for rule in default_rules:
        alert_manager.add_alert_rule(rule)