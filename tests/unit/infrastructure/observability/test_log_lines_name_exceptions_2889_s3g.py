"""The observability area's log lines name an exception by its class, never its
text (#2889 S3g).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, and the
class name is on the site's line. How each site is driven is stated on its
case. What the caller gets back (a health record, a buffered metric, an alert)
is asserted beside it, unchanged.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.infrastructure.health import (
    component_monitor as component_monitor_module,
)
from faultmaven.infrastructure.health.component_monitor import (
    ComponentHealthMonitor,
    HealthStatus,
)
from faultmaven.infrastructure.logging.helpers import log_error_with_context
from faultmaven.infrastructure.observability.alerting import (
    AlertChannel,
    AlertChannelConfig,
    AlertManager,
    AlertRule,
    AlertSeverity,
)
from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector
from faultmaven.infrastructure.observability.sla_monitor import SLAMonitor
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


# ── infrastructure/health/component_monitor.py ────────────────────────────


async def test_a_raising_probe_task_is_named_by_class(lines, monkeypatch):
    """Driven through ``check_components``, the one probe's task raising past
    ``check_component_health`` (stubbed to raise). The component is still
    recorded UNHEALTHY by ``_record_abandoned_probe``."""
    monitor = ComponentHealthMonitor()

    async def _raises(_name):
        raise RuntimeError(f"probe broke {MARKER}")

    monkeypatch.setattr(monitor, "check_component_health", _raises)
    results = await monitor.check_components(["database"])
    assert results["database"].status == HealthStatus.UNHEALTHY
    _named(lines(), "Health check failed for database", "database: RuntimeError")


class _RefusingRegistry:
    """A metrics export whose every publish raises with the marker."""

    def labels(self, **_kwargs):
        raise RuntimeError(f"metrics registry broken {MARKER}")

    def remove(self, *_args):
        raise RuntimeError(f"metrics registry broken {MARKER}")


def test_a_refused_gauge_publish_is_named_by_class(lines, monkeypatch):
    """Driven through ``get_overall_health_status`` with the metrics registry
    refusing every publish: four sweeps (one reported, three suppressed), then
    one more once the window has passed. The class replaces the text, the
    ``since`` suffix stays, and the health read still answers."""
    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.component_health_status",
        _RefusingRegistry(),
        raising=False,
    )
    monitor = ComponentHealthMonitor()
    for _ in range(4):
        assert monitor.get_overall_health_status()
    monkeypatch.setattr(
        component_monitor_module, "_GAUGE_PUBLISH_WARN_INTERVAL_SECONDS", 0.0
    )
    assert monitor.get_overall_health_status()
    rendered = lines()
    _named(rendered, "Could not publish health gauge for", "RuntimeError")
    _named(
        rendered,
        "component_health_status is not being published for",
        ": RuntimeError. The series",
        ": RuntimeError (3 further failures suppressed). The series",
    )


# ── infrastructure/logging/helpers.py ─────────────────────────────────────


def test_the_error_helper_names_the_exception_by_class(lines):
    """``log_error_with_context``, called from the ``except`` of an operation
    that raised. ``exception_type``, the caller's context and the frames stay
    on the line."""
    logger = logging.getLogger("faultmaven.tests.s3g")
    try:
        raise ValueError(f"bad value {MARKER}")
    except ValueError as e:
        log_error_with_context(logger, "Failed to process case", e, case_id="c-1")
    failed = _named(
        lines(), "Failed to process case", "Failed to process case: ValueError"
    )
    assert failed["exception_type"] == "ValueError"
    assert failed["case_id"] == "c-1"
    assert failed["frames"].startswith("ValueError")


def test_the_error_helper_cleans_a_class_name_that_is_not_an_identifier(lines):
    """``log_error_with_context`` with an exception whose class ``__name__``
    carries the marker and is not a plain identifier: ``exception_type`` holds
    the cleaned name, and the raw name is on no rendered line."""
    odd = type(f"db password={MARKER}", (Exception,), {})
    logger = logging.getLogger("faultmaven.tests.s3g")
    try:
        raise odd("plain")
    except Exception as e:
        log_error_with_context(logger, "Failed to process case", e)
    failed = _named(
        lines(), "Failed to process case", "Failed to process case: Exception"
    )
    assert failed["exception_type"] == "Exception"


# ── infrastructure/observability/sla_monitor.py ───────────────────────────


async def test_a_failed_sla_evaluation_is_named_by_class(lines, monkeypatch):
    """Driven through ``_sla_monitor_task`` at a zero interval, ``_evaluate_sla``
    stubbed to raise and to end the loop. Every enabled SLA is still tried."""
    monitor = SLAMonitor()
    monitor._config["monitoring_interval_seconds"] = 0
    tried = []

    async def _raises(sla):
        tried.append(sla.sla_id)
        monitor._background_tasks_running = False
        raise RuntimeError(f"evaluation broke {MARKER}")

    monkeypatch.setattr(monitor, "_evaluate_sla", _raises)
    monitor._background_tasks_running = True
    try:
        await monitor._sla_monitor_task()
    finally:
        monitor._executor.shutdown(wait=True)
    enabled = [i for i, sla in monitor._sla_definitions.items() if sla.enabled]
    assert tried == enabled
    _named(lines(), f"Failed to evaluate SLA {enabled[0]}", "RuntimeError")


# ── infrastructure/observability/metrics_collector.py ─────────────────────


def test_a_failed_metric_record_is_named_by_class(lines, monkeypatch):
    """``record_metric``, its real-time analytics update stubbed to raise. The
    metric is still buffered."""
    collector = MetricsCollector()

    def _raises(_metric):
        raise RuntimeError(f"analytics broke {MARKER}")

    monkeypatch.setattr(collector, "_update_real_time_analytics", _raises)
    try:
        collector.record_metric("svc", "op", 1.0)
    finally:
        collector._executor.shutdown(wait=True)
    assert len(collector._metrics_buffer) == 1
    _named(lines(), "Failed to record metric", "Failed to record metric: RuntimeError")


# ── infrastructure/observability/alerting.py ──────────────────────────────


async def test_a_failed_alert_notification_is_named_by_class(lines):
    """Driven through ``evaluate_metric``, a rule on the webhook channel whose
    handler raises; the notification task it schedules is awaited. The alert
    is still triggered."""
    alerting = SimpleNamespace(
        alert_webhook_url=None,
        alert_to_emails=None,
        alert_from_email=None,
        smtp_host=None,
        smtp_port=None,
    )
    manager = AlertManager(settings=SimpleNamespace(alerting=alerting))
    manager.configure_channel(
        AlertChannel.WEBHOOK, AlertChannelConfig(channel=AlertChannel.WEBHOOK)
    )
    manager.add_notification_handler(
        AlertChannel.WEBHOOK,
        AsyncMock(side_effect=RuntimeError(f"webhook refused {MARKER}")),
    )
    manager.add_alert_rule(
        AlertRule(
            rule_id="r-1",
            name="High latency",
            description="latency over threshold",
            metric_name="latency_ms",
            condition="greater_than",
            threshold_value=100.0,
            severity=AlertSeverity.HIGH,
            channels=[AlertChannel.WEBHOOK],
        )
    )
    before = asyncio.all_tasks()
    triggered = manager.evaluate_metric("latency_ms", 250.0)
    await asyncio.gather(*(asyncio.all_tasks() - before))
    assert [alert.rule_id for alert in triggered] == ["r-1"]
    _named(
        lines(),
        "Failed to send alert notification to webhook",
        "webhook: RuntimeError",
    )
