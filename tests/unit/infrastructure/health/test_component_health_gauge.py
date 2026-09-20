"""`component_health_status` and `/health`'s body are one derivation (#1547).

`/health` is honest and answers 200 by design — it is the liveness surface, so
the Kubernetes probes that read it cannot act on a dependency outage, and
restarting a pod would not fix a shared primary anyway. The verdict was
correct and unconsumed. `component_health_status` is what an alerting rule
reads instead, and
its entire value rests on being the *same* read as the body's: a metric that
grades components separately is a second opinion, and the day the two disagree
is the day the alert is wrong in whichever direction nobody checked.

So these tests do not assert that a gauge exists. They assert that no
arrangement of component state makes the metric and the body say different
things — including the arrangement that used to: a probe abandoned by the
sweep budget, where the body's component map said `unhealthy` and its own
`summary` did not.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from faultmaven.infrastructure.health import (
    component_monitor as component_monitor_module,
)
from faultmaven.infrastructure.health.component_monitor import (
    ComponentHealthMonitor,
    HealthStatus,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# An isolated real gauge
#
# The shipped one is a module-level singleton on the default REGISTRY, and it
# is a NoOpMetric unless ENABLE_METRICS was true when the shim was imported —
# neither of which a test can arrange after the fact. So the shim's export is
# replaced with a real Gauge of the same shape on a registry of this test's
# own, which is also what lets each test read the series back.
# --------------------------------------------------------------------------


@pytest.fixture
def published(monkeypatch):
    """Publish into a private registry; return a reader for its samples.

    `prometheus-client` is a cloud extra, so the tests that need a real
    registry skip on the standalone leg. Deliberately scoped to the fixture
    rather than the module: the derivation, the abandoned-probe fix and the
    degradation path are all testable without it, and those run everywhere.
    """
    prometheus_client = pytest.importorskip("prometheus_client")
    registry = prometheus_client.CollectorRegistry()
    gauge = prometheus_client.Gauge(
        "component_health_status",
        "isolated stand-in for the shim's module-level gauge",
        labelnames=["component", "fatal", "fails_per_replica"],
        registry=registry,
    )
    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.component_health_status",
        gauge,
        raising=False,
    )

    def read() -> Dict[tuple, float]:
        return {
            (
                sample.labels["component"],
                sample.labels["fatal"],
                sample.labels["fails_per_replica"],
            ): sample.value
            for metric in registry.collect()
            for sample in metric.samples
        }

    return read


def _grades(samples: Dict[tuple, float]) -> Dict[str, float]:
    return {component: value for (component, _, _), value in samples.items()}


def _firing(samples: Dict[tuple, float]) -> set:
    """What `component_health_status{fatal="true"} == 1` selects.

    The alert rule that lives in faultmaven-enterprise-infra, written out as
    the set it would return, so this repo can hold it to the body.
    """
    return {
        component
        for (component, fatal, _), value in samples.items()
        if fatal == "true" and value == 1.0
    }


def _all(monitor: ComponentHealthMonitor, status: HealthStatus) -> None:
    for health in monitor.component_health.values():
        health.status = status


# --------------------------------------------------------------------------
# Absence is not a value
# --------------------------------------------------------------------------


def test_every_registered_component_is_published_including_the_healthy_ones(published):
    """A series that only appears when something breaks is unreadable.

    "No series" and "healthy" have to look different, or an alert on the
    absence of trouble cannot tell a working deployment from an unscraped one.
    """
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)

    monitor.get_overall_health_status()

    samples = published()
    assert set(_grades(samples)) == set(monitor.component_health)
    assert set(samples.values()) == {3.0}
    # Positive control: the fixture really is capturing something.
    assert len(samples) == len(monitor.component_health) >= 8


def test_an_unprobed_process_reports_unknown_rather_than_nothing(published):
    """Freshly constructed, before any probe has run: 0, not an empty scrape."""
    monitor = ComponentHealthMonitor()

    monitor.get_overall_health_status()

    assert set(published().values()) == {0.0}


# --------------------------------------------------------------------------
# The alert expression and the body's verdict are the same set
# --------------------------------------------------------------------------


def test_the_alert_expression_selects_exactly_what_the_body_calls_fatal(published):
    """Every near-miss the body distinguishes, the gauge distinguishes too.

    The three traps, all live at once: a fatal component that is DEGRADED
    still serves; a fatal component that is UNKNOWN was never determined and
    "we could not tell" must not page; an UNHEALTHY component that is not
    fatal degrades answers and nothing more.
    """
    monitor = ComponentHealthMonitor()
    monitor.register_component("degrading_but_fatal", fatal=True)
    monitor.register_component("undeterminable_but_fatal", fatal=True)
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY
    monitor.component_health["vector_store"].status = HealthStatus.UNHEALTHY
    monitor.component_health["degrading_but_fatal"].status = HealthStatus.DEGRADED
    monitor.component_health["undeterminable_but_fatal"].status = HealthStatus.UNKNOWN

    status, summary = monitor.get_overall_health_status()

    samples = published()
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]
    assert _firing(samples) == set(summary["fatal_unhealthy"])
    # And the near-misses are legible rather than merely excluded, so a rule
    # can be written for them without a second source.
    grades = _grades(samples)
    assert grades["vector_store"] == 1.0
    assert grades["degrading_but_fatal"] == 2.0
    assert grades["undeterminable_but_fatal"] == 0.0


def test_the_classification_travels_as_labels_so_no_rule_names_a_component(published):
    """The fatal set is data. A rule selects it; it does not restate it."""
    monitor = ComponentHealthMonitor()
    monitor.register_component("scratch_disk", fatal=True, fails_per_replica=True)
    _all(monitor, HealthStatus.HEALTHY)

    monitor.get_overall_health_status()

    samples = published()
    fatal_labelled = {c for (c, fatal, _) in samples if fatal == "true"}
    assert fatal_labelled == set(monitor.fatal_components)
    readiness_labelled = {
        c for (c, fatal, per_replica) in samples if fatal == per_replica == "true"
    }
    assert readiness_labelled == set(monitor.readiness_fatal_components)
    # The page-a-human set: fatal, and no readiness probe can shed around it.
    assert {c for (c, f, p) in samples if f == "true" and p == "false"} == {"database"}


# --------------------------------------------------------------------------
# The abandoned probe — the case that used to make /health disagree with
# itself, and the case the gauge exists for (a hung primary)
# --------------------------------------------------------------------------


def _hang_one(monitor: ComponentHealthMonitor, monkeypatch, hung: str) -> None:
    """Every probe answers HEALTHY except `hung`, which never returns."""

    async def _probe(component_name: str) -> Dict[str, Any]:
        if component_name == hung:
            await asyncio.sleep(3600)
        return {"status": HealthStatus.HEALTHY, "metadata": {}}

    monkeypatch.setattr(monitor, "_perform_health_check", _probe)


async def test_an_abandoned_probe_reaches_the_gauge_and_the_summary(
    published, monkeypatch
):
    """A hung database is the whole point, and it used to be invisible.

    The sweep cancels a probe that blows its budget. That verdict was reported
    only in the returned map, never written back, so `get_overall_health_status`
    — which reads the stored records — still described the previous sweep and
    left the component out of `fatal_unhealthy`. Body versus body, one
    endpoint, two answers; the gauge would have inherited the wrong one.
    """
    monkeypatch.setattr(
        component_monitor_module, "_ALL_COMPONENTS_TIMEOUT_SECONDS", 0.05
    )
    monitor = ComponentHealthMonitor()
    _hang_one(monitor, monkeypatch, "database")

    results = await monitor.check_all_components()
    status, summary = monitor.get_overall_health_status()

    assert results["database"].status is HealthStatus.UNHEALTHY
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]
    assert _firing(published()) == {"database"}
    # And it counts against availability: an abandoned probe is a failed
    # probe, so leaving it out of the window overstated the 24h figure.
    assert monitor.component_health["database"].probe_failures_24h == 1
    assert monitor.component_health["database"].probe_availability_24h == 0.0


async def test_a_probe_whose_task_raises_is_recorded_the_same_way(
    published, monkeypatch
):
    """The other abandonment: the task escapes with a non-Exception."""
    monitor = ComponentHealthMonitor()

    async def _explode(component_name: str):
        raise BaseException("not an Exception, so check_component_health misses it")

    monkeypatch.setattr(monitor, "check_component_health", _explode)

    results = await monitor.check_all_components()
    _, summary = monitor.get_overall_health_status()

    assert results["database"].status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]
    assert _firing(published()) == {"database"}


async def test_the_returned_map_and_the_stored_record_are_one_object(monkeypatch):
    """There is one `ComponentHealth` per component, however the probe ended.

    The divergence above was possible because the abandoned paths built a
    fresh record. Copying fields by hand is the drift shape whatever the
    fields are — those two constructors already dropped `metadata` and the
    24h probe figures — so the fix is that no copy is made at all.

    No gauge here on purpose: this is the half of the fix that is about
    `/health` agreeing with itself, and it has to be checked on the
    standalone leg too, where `prometheus-client` is not installed.
    """
    monkeypatch.setattr(
        component_monitor_module, "_ALL_COMPONENTS_TIMEOUT_SECONDS", 0.05
    )
    monitor = ComponentHealthMonitor()
    _hang_one(monitor, monkeypatch, "database")

    results = await monitor.check_all_components()
    status, summary = monitor.get_overall_health_status()

    for name, health in results.items():
        assert health is monitor.component_health[name]
    assert results["database"].status is HealthStatus.UNHEALTHY
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]


# --------------------------------------------------------------------------
# The scrape hook
# --------------------------------------------------------------------------


def test_the_scrape_hook_publishes_without_anyone_calling_health(published):
    """Prometheus scrapes /metrics, not /health. The hook bridges that."""
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    monitor.publish_health_gauges()

    assert _firing(published()) == {"database"}


def test_the_scrape_hook_has_no_derivation_of_its_own(published, monkeypatch):
    """It re-runs the body's computation; it does not reimplement it.

    Pinning this is the guard against the shape #1548 removed elsewhere: a
    hook that grew its own loop over `component_health` would keep passing
    every assertion above while being free to drift from the body tomorrow.
    """
    monitor = ComponentHealthMonitor()
    calls: list = []
    monkeypatch.setattr(
        monitor,
        "get_overall_health_status",
        lambda: calls.append(1) or (HealthStatus.HEALTHY, {}),
    )

    monitor.publish_health_gauges()

    assert calls == [1]
    assert published() == {}


def test_the_hook_signature_matches_what_register_scrape_hook_accepts():
    """`Callable[[], None]` — no arguments, and nothing read from the result."""
    monitor = ComponentHealthMonitor()
    assert monitor.publish_health_gauges() is None


# --------------------------------------------------------------------------
# Housekeeping: a series nothing writes again must not be left firing
# --------------------------------------------------------------------------


def test_a_component_that_stops_being_fatal_does_not_leave_a_stale_series(published):
    """The label pair carries the classification, so flipping it moves series.

    Left behind, the old pair is a value frozen at the instant of the flip,
    with no restart in sight to clear it — a permanent page.
    """
    monitor = ComponentHealthMonitor()
    monitor.register_component("scratch_disk", fatal=True)
    monitor.component_health["scratch_disk"].status = HealthStatus.UNHEALTHY
    monitor.get_overall_health_status()
    assert ("scratch_disk", "true", "false") in published()

    monitor.register_component("scratch_disk", fatal=False)
    monitor.component_health["scratch_disk"].status = HealthStatus.UNHEALTHY
    monitor.get_overall_health_status()

    samples = published()
    assert ("scratch_disk", "true", "false") not in samples
    assert samples[("scratch_disk", "false", "false")] == 1.0
    assert _firing(samples) == set()


# --------------------------------------------------------------------------
# Degradation: metrics off is the default everywhere but Cloud
# --------------------------------------------------------------------------


def test_publishing_is_a_no_op_when_metrics_are_disabled():
    """No fixture here: the shipped shim export is a NoOpMetric in tests."""
    monitor = ComponentHealthMonitor()
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    status, summary = monitor.get_overall_health_status()

    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]


def test_a_broken_gauge_never_costs_the_health_read(monkeypatch):
    """`/health` is a liveness surface; metrics must not be able to blind it.

    The route falls back to a bodyless "monitoring unavailable" answer on any
    exception, so an exception raised while publishing would trade the honest
    verdict for the metric — losing both. The amplifier does not get to break
    the thing it amplifies.
    """

    class _Broken:
        def labels(self, **_kwargs):
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.component_health_status",
        _Broken(),
        raising=False,
    )
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    status, summary = monitor.get_overall_health_status()

    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]


def test_every_health_status_has_a_gauge_value():
    """The one way the publish could realistically raise, caught at test time.

    A new `HealthStatus` member with no mapping would `KeyError` on the
    liveness path — survivable because of the guard above, but it would take
    that component's series with it silently.
    """
    values = component_monitor_module._HEALTH_STATUS_GAUGE_VALUES
    assert set(values) == set(HealthStatus)
    # Distinct, and ordered worst-to-best like `sla_status` beside it, so a
    # rule can compare rather than enumerate.
    assert sorted(values.values()) == [0, 1, 2, 3]
    assert values[HealthStatus.UNHEALTHY] < values[HealthStatus.DEGRADED]
    assert values[HealthStatus.DEGRADED] < values[HealthStatus.HEALTHY]
    # UNKNOWN is not UNHEALTHY: "could not tell" must be separable (#1524).
    assert values[HealthStatus.UNKNOWN] != values[HealthStatus.UNHEALTHY]
