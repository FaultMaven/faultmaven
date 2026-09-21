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
import logging
from typing import Any, Dict

import pytest

from faultmaven.infrastructure.health import (
    component_monitor as component_monitor_module,
)
from faultmaven.infrastructure.health.component_monitor import (
    ComponentHealthMonitor,
    HealthStatus,
)
from tests.unit.infrastructure.health.probe_deadlines import (
    probe_that_ignores_cancellation,
    shipped_ratio,
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


def _paging(samples: Dict[tuple, float]) -> set:
    """The rule `docs/operations/monitoring/README.md` publishes, verbatim:

        component_health_status{fatal="true", fails_per_replica="false"} == 1

    Written out as the set it would select, so this repo holds the DOCUMENTED
    expression to the body rather than a convenient approximation of it. The
    `fails_per_replica="false"` half is not decoration: a fatal component that
    CAN fail on one pod is readiness's job, and the page rule must skip it.
    """
    return {
        component
        for (component, fatal, per_replica), value in samples.items()
        if fatal == "true" and per_replica == "false" and value == 1.0
    }


def _fatal_and_down(samples: Dict[tuple, float]) -> set:
    """`{fatal="true"} == 1` — the gauge's spelling of `fatal_unhealthy`.

    A DIFFERENT set from `_paging`: this one is what the body's
    `summary.fatal_unhealthy` means, and it is a superset. The two coincide
    only while `database` is the sole fatal component, which is exactly why
    both exist here.
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
    assert _fatal_and_down(samples) == set(summary["fatal_unhealthy"])
    # And the near-misses are legible rather than merely excluded, so a rule
    # can be written for them without a second source.
    grades = _grades(samples)
    assert grades["vector_store"] == 1.0
    assert grades["degrading_but_fatal"] == 2.0
    assert grades["undeterminable_but_fatal"] == 0.0


def test_a_truthy_non_bool_flag_still_renders_as_a_selectable_label(published):
    """`str(1).lower()` is `"1"`, and `{fatal="true"}` misses it silently.

    Every call site passes a real bool today, so this guards the coercion
    rather than a live bug — cheap insurance on the label the whole alerting
    story selects by.
    """
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["database"].fatal = 1  # truthy, not a bool
    monitor.component_health["database"].fails_per_replica = 0
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    monitor.get_overall_health_status()

    samples = published()
    assert ("database", "true", "false") in samples
    assert _paging(samples) == {"database"}


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
# The page rule is NOT the same set as `fatal_unhealthy`
# --------------------------------------------------------------------------


def test_the_documented_page_rule_skips_what_readiness_can_shed(published):
    """`fatal_unhealthy` is a superset of what should wake a human.

    A component that is fatal AND can fail on one pod is readiness's job:
    `/readiness` 503s, the pod leaves the Service, a sibling serves. Paging on
    it would be paging for something already being handled. The body still
    lists it under `fatal_unhealthy` — correctly, it IS fatal — so the two
    sets genuinely differ, and a test that checked only `{fatal="true"}` would
    pass while the documented rule paged nobody.
    """
    monitor = ComponentHealthMonitor()
    monitor.register_component("scratch_disk", fatal=True, fails_per_replica=True)
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["scratch_disk"].status = HealthStatus.UNHEALTHY

    _, summary = monitor.get_overall_health_status()

    samples = published()
    assert set(summary["fatal_unhealthy"]) == {"scratch_disk"}
    assert _fatal_and_down(samples) == {"scratch_disk"}
    # ... and the page rule stays silent, because readiness handles this one.
    assert _paging(samples) == set()


def test_the_page_rule_fires_for_a_shared_fatal_dependency(published):
    """The other half: `database` is fatal and shared, so it does page."""
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    _, summary = monitor.get_overall_health_status()

    samples = published()
    assert _paging(samples) == {"database"} == set(summary["fatal_unhealthy"])


# --------------------------------------------------------------------------
# The abandoned-probe arm
#
# What this arm covers, stated as measured rather than as assumed: a probe
# that OUTLIVES the sweep budget, and a task that raises past
# `check_component_health`'s `except`. A hung `database` is NOT it — that is
# caught by the per-probe deadline, whose error arm already wrote back before
# this change (measured identical with and without it). Every shipped probe
# carries `_PROBE_TIMEOUT_SECONDS` under `_ALL_COMPONENTS_TIMEOUT_SECONDS`, so
# the arm is a BACKSTOP, and these tests drive it the only way the shipped
# ordering allows: a probe that does not observe cancellation.
# --------------------------------------------------------------------------


def test_the_shipped_constants_keep_this_arm_a_backstop():
    """If these ever invert, the sweep arm becomes a live path — say so here.

    The claim "no shipped probe reaches the sweep-budget arm" rests entirely
    on this ordering, so it is pinned rather than assumed. (The neighbouring
    probe-behaviour suite pins the same relation; this is the copy that
    carries the reason the abandoned-probe arm depends on it.)
    """
    assert (
        component_monitor_module._PROBE_TIMEOUT_SECONDS
        < component_monitor_module._ALL_COMPONENTS_TIMEOUT_SECONDS
    )


async def test_a_probe_outliving_the_sweep_budget_is_written_back(monkeypatch):
    """The arm, driven at the shipped ordering.

    No gauge: this is the half about `/health` agreeing with itself, and it
    must run on the standalone leg where `prometheus-client` is absent.
    """
    shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()
    probe_that_ignores_cancellation(monitor, monkeypatch, "database")

    results = await monitor.check_all_components()
    status, summary = monitor.get_overall_health_status()

    assert results["database"].status is HealthStatus.UNHEALTHY
    assert "sweep budget" in (results["database"].last_error or "")
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]
    # One object per component, however the probe ended — no hand-copied
    # fields to fall behind `ComponentHealth`.
    for name, health in results.items():
        assert health is monitor.component_health[name]
    # An abandoned probe is a failed probe; omitting it overstated the window.
    assert monitor.component_health["database"].probe_failures_24h == 1
    assert monitor.component_health["database"].probe_availability_24h == 0.0


async def test_a_probe_outliving_the_sweep_budget_reaches_the_gauge(
    published, monkeypatch
):
    """...and the metric says what the body says."""
    shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()
    probe_that_ignores_cancellation(monitor, monkeypatch, "database")

    await monitor.check_all_components()
    _, summary = monitor.get_overall_health_status()

    assert _paging(published()) == {"database"} == set(summary["fatal_unhealthy"])


async def test_a_probe_whose_task_raises_is_recorded_the_same_way(monkeypatch):
    """The other arm: the task escapes with a non-Exception.

    `CancelledError` is not an `Exception`, so `check_component_health`'s own
    handler never sees it. No gauge, so this runs on both CI legs.
    """
    monitor = ComponentHealthMonitor()

    async def _explode(component_name: str):
        raise BaseException("not an Exception, so check_component_health misses it")

    monkeypatch.setattr(monitor, "check_component_health", _explode)

    results = await monitor.check_all_components()
    _, summary = monitor.get_overall_health_status()

    assert results["database"].status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]
    for name, health in results.items():
        assert health is monitor.component_health[name]


async def test_the_task_raised_arm_reaches_the_gauge(published, monkeypatch):
    """The gauge half of the above, for the leg that has a registry."""
    monitor = ComponentHealthMonitor()

    async def _explode(component_name: str):
        raise BaseException("not an Exception")

    monkeypatch.setattr(monitor, "check_component_health", _explode)

    await monitor.check_all_components()
    monitor.get_overall_health_status()

    assert _paging(published()) == {"database"}


# --------------------------------------------------------------------------
# An older sweep must never overwrite a newer observation
#
# The verdict is timed by when the SWEEP gave up, not by when the probe was
# taken, and /health has four callers whose sweeps overlap routinely. Without
# an ordering guard the older sweep's abandon lands last and clobbers a
# success — a false page on the signal this PR adds, which `main` does not
# have.
# --------------------------------------------------------------------------


async def _overlapping_sweeps(monitor, monkeypatch):
    """Sweep A's `database` probe stalls; sweep B completes; then A gives up."""
    calls = {"database": 0}

    async def _probe(component_name: str) -> Dict[str, Any]:
        if component_name == "database":
            calls["database"] += 1
            if calls["database"] == 1:  # sweep A only
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(3600)
            return {
                "status": HealthStatus.HEALTHY,
                "metadata": {"rls_posture": "enforced"},
            }
        return {"status": HealthStatus.HEALTHY, "metadata": {}}

    monkeypatch.setattr(monitor, "_perform_health_check", _probe)
    sweep_a = asyncio.ensure_future(monitor.check_all_components())
    await asyncio.sleep(0.02)  # A is in flight
    await monitor.check_all_components()  # B completes fully
    await sweep_a  # A's abandon lands LAST


async def test_a_stale_sweep_does_not_clobber_a_newer_success(monkeypatch):
    """Measured before the guard: `unhealthy`, and the page fired, while up."""
    shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()

    await _overlapping_sweeps(monitor, monkeypatch)

    database = monitor.component_health["database"]
    _, summary = monitor.get_overall_health_status()
    assert database.status is HealthStatus.HEALTHY
    assert summary["fatal_unhealthy"] == []
    # `metadata` carries the RLS posture, and the abandon arm blanks it.
    assert database.metadata == {"rls_posture": "enforced"}


async def test_a_stale_sweep_does_not_fire_the_page_rule(published, monkeypatch):
    """The same run, read off the gauge: nothing pages while the primary is up."""
    shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()

    await _overlapping_sweeps(monitor, monkeypatch)
    monitor.get_overall_health_status()

    samples = published()
    assert _paging(samples) == set()
    assert _fatal_and_down(samples) == set()


async def test_the_first_sweep_to_abandon_still_records(monkeypatch):
    """The guard declines only a STALE write, never every write.

    Without this the fix could be "never record anything" and every test above
    that asserts the arm works would have to be wrong for it to show.
    """
    shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()
    probe_that_ignores_cancellation(monitor, monkeypatch, "database")

    await monitor.check_all_components()

    assert monitor.component_health["database"].status is HealthStatus.UNHEALTHY


# --------------------------------------------------------------------------
# `fails_per_replica` on the component detail surface (#1543's review)
# --------------------------------------------------------------------------


def test_component_metrics_carry_both_declarations():
    """`fatal` alone never answered "will /readiness 503 for this?".

    Readiness-fatal is the conjunction, so a surface carrying one half of it
    leaves an operator unable to tell. No gauge, so this runs on both legs.
    """
    monitor = ComponentHealthMonitor()
    monitor.register_component("scratch_disk", fatal=True, fails_per_replica=True)

    shared = monitor.get_component_metrics("database")
    per_pod = monitor.get_component_metrics("scratch_disk")

    assert shared["fatal"] is True
    assert shared["fails_per_replica"] is False
    assert per_pod["fatal"] is True
    assert per_pod["fails_per_replica"] is True
    # And the pair agrees with the set /readiness actually reads.
    assert per_pod["component_name"] in monitor.readiness_fatal_components
    assert shared["component_name"] not in monitor.readiness_fatal_components


# --------------------------------------------------------------------------
# The scrape hook
# --------------------------------------------------------------------------


def test_the_scrape_hook_publishes_without_anyone_calling_health(published):
    """Prometheus scrapes /metrics, not /health. The hook bridges that."""
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    monitor.publish_health_gauges()

    assert _fatal_and_down(published()) == {"database"}


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
    assert _fatal_and_down(samples) == set()


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


# --------------------------------------------------------------------------
# `/health`'s fallback arm — the one place the body and the gauge can diverge
# --------------------------------------------------------------------------


async def test_no_component_failure_shape_reaches_the_health_fallback_body(
    monkeypatch,
):
    """Drive `/health` itself, not `check_all_components` (#1568 item 2).

    `health_check`'s `except Exception` returns a body with no `summary`, no
    `components` and no `fatal_unhealthy` while `/metrics` keeps serving the
    last sweep's per-component verdict — so it is the single arrangement in
    which the two disagree, and the claim "they cannot" is only as good as
    that arm being unreachable.

    Each shape below is a way a dependency, a probe or the metrics registry
    actually fails; none of them must reach it. A direct call to
    `check_all_components` would prove the monitor's logic and say nothing
    about the endpoint's `try`, which is what the disagreement lives in — so
    this drives the endpoint function.
    """
    from faultmaven import main as main_module
    from faultmaven.infrastructure.health import component_monitor as cm_module

    monitor = ComponentHealthMonitor()
    monkeypatch.setattr(cm_module, "component_monitor", monitor)

    def _reached_the_fallback(body: Dict[str, Any]) -> bool:
        return body.get("error") == "Enhanced health monitoring unavailable"

    class _NotAnException(BaseException):
        """Past `check_component_health`'s `except Exception`, deliberately."""

    async def _raises(_name: str) -> Dict[str, Any]:
        raise RuntimeError("dependency down")

    async def _raises_base(_name: str) -> Dict[str, Any]:
        raise _NotAnException("not an Exception")

    async def _healthy(_name: str) -> Dict[str, Any]:
        return {"status": HealthStatus.HEALTHY, "metadata": {}}

    class _RefusingRegistry:
        def labels(self, **_kwargs):
            raise RuntimeError("metrics registry broken")

        def remove(self, *_args):
            raise RuntimeError("metrics registry broken")

    # 1. every probe raises — `check_component_health` catches and writes back.
    monkeypatch.setattr(monitor, "_perform_health_check", _raises)
    body = await main_module.health_check()
    assert not _reached_the_fallback(body), "a probe raising Exception"
    assert body["summary"]["component_counts"]["unhealthy"] == len(
        monitor.component_health
    )

    # 2. a probe raises past that `except` — the sweep's own `task.exception()`
    #    arm handles it, and `_record_abandoned_probe` writes back.
    monkeypatch.setattr(monitor, "_perform_health_check", _raises_base)
    body = await main_module.health_check()
    assert not _reached_the_fallback(body), "a probe raising BaseException"
    assert body["summary"]["fatal_unhealthy"] == ["database"]

    # 3. the metrics registry refuses the publish — the publisher swallows it,
    #    so `/health` still answers from the same read the gauge failed on.
    monkeypatch.setattr(monitor, "_perform_health_check", _healthy)
    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.component_health_status",
        _RefusingRegistry(),
        raising=False,
    )
    body = await main_module.health_check()
    assert not _reached_the_fallback(body), "the gauge publish raising"
    assert body["status"] == HealthStatus.HEALTHY.value

    # POSITIVE CONTROL. Without it every assertion above could hold because
    # the arm is unreachable from this test rather than from production —
    # a probe that cannot fail reads exactly like a probe that passed.
    async def _sweep_explodes() -> Dict[str, Any]:
        raise RuntimeError("the sweep itself failed")

    monkeypatch.setattr(monitor, "check_all_components", _sweep_explodes)
    body = await main_module.health_check()
    assert _reached_the_fallback(body), "control: the arm must be reachable"
    assert "summary" not in body and "components" not in body


# --------------------------------------------------------------------------
# A failed publish must not be silent (#1568 item 3)
#
# `debug` was the original and only channel, and the deployment does not emit
# it — so the failure mode was "the series disappears and nothing says why",
# which is the absence-reads-as-fine shape the whole metric argues against.
# The flood argument against `warning` is real (the caller runs on every
# liveness probe), so the answer is a rate limit, not a level change.
# --------------------------------------------------------------------------


class _RefusingRegistry:
    """A metrics export that rejects every publish, switchably."""

    def __init__(self, failing: bool = True):
        self.failing = failing

    def labels(self, **_kwargs):
        if self.failing:
            raise RuntimeError("metrics registry broken")
        return self

    def set(self, _value):
        return None

    def remove(self, *_args):
        if self.failing:
            raise RuntimeError("metrics registry broken")


def _install_registry(monkeypatch, registry) -> None:
    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.component_health_status",
        registry,
        raising=False,
    )


def _publish_records(caplog) -> list:
    return [
        record
        for record in caplog.records
        if "component_health_status is not being published" in record.getMessage()
    ]


def _publish_warnings(caplog) -> list:
    return [record.getMessage() for record in _publish_records(caplog)]


def _recovery_warnings(caplog) -> list:
    return [
        record.getMessage()
        for record in caplog.records
        if "component_health_status publishing recovered" in record.getMessage()
    ]


def _window_has_passed(monkeypatch) -> None:
    """Make the next report unthrottled, without sleeping five minutes."""
    monkeypatch.setattr(
        component_monitor_module, "_GAUGE_PUBLISH_WARN_INTERVAL_SECONDS", 0.0
    )


def test_a_failed_publish_warns_immediately_then_is_rate_limited(monkeypatch, caplog):
    """First failure at WARNING; the rest of the interval is quiet.

    Immediately, because a fault that clears before the interval elapses
    would otherwise never be reported at all — and one scrape of a missing
    series is exactly the hole this announces.
    """
    monitor = ComponentHealthMonitor()
    _install_registry(monkeypatch, _RefusingRegistry())

    with caplog.at_level(logging.WARNING, logger=component_monitor_module.__name__):
        monitor.get_overall_health_status()
        first = _publish_records(caplog)
        assert len(first) == len(monitor.component_health)
        # The level is the point of the change, so assert it rather than the
        # text: `debug` alone is what made a vanished series silent.
        assert {record.levelname for record in first} == {"WARNING"}

        caplog.clear()
        for _ in range(3):
            monitor.get_overall_health_status()
        assert _publish_warnings(caplog) == []

        # Once the interval elapses the next failure reports again, and says
        # how many it swallowed — a throttle that hides the scale is a second
        # way for the log to understate what happened.
        _window_has_passed(monkeypatch)
        monitor.get_overall_health_status()
        again = _publish_warnings(caplog)
        assert len(again) == len(monitor.component_health)
        assert all("(3 further failures suppressed)" in message for message in again)


#: The shipped `livenessProbe.periodSeconds` for faultmaven-api, which is what
#: calls `/health` in steady state and therefore what drives the publisher
#: (`faultmaven-enterprise-infra`, `base/faultmaven-api/deployment.yaml`).
#: `/health/dependencies` and the startupProbe add to that rate; none of them
#: lower it.
_LIVENESS_PROBE_PERIOD_SECONDS = 30.0


def test_the_warn_interval_stays_between_flood_and_silence():
    """The interval is the whole compromise, so pin the range it lives in.

    Both bounds are anchored, because `0 < interval <= 3600` — the first
    version of this assertion — bites at NEITHER end:

    * At `interval = 1.0` it passes, and since the publisher runs once per
      liveness probe every failure is already more than an interval apart.
      Every failure then warns: the unthrottled flood, with the guard green.
      So the lower bound has to be anchored to the CALL RATE, not to zero —
      an interval at or below the probe period throttles nothing.
    * At `3600.0` it passes while the docstring calls an hour "the silence".
      `<=` admitting the value a bound exists to exclude is the same defect
      this PR tightened one file over, in
      `test_the_sweep_budget_fits_inside_the_startup_probe_timeout`.
    """
    interval = component_monitor_module._GAUGE_PUBLISH_WARN_INTERVAL_SECONDS
    assert interval >= 2 * _LIVENESS_PROBE_PERIOD_SECONDS, (
        "an interval at or near the publisher's call rate is not a throttle: "
        "every failure would be more than an interval apart and warn"
    )
    assert interval < 3600.0, (
        "an hour or more is the silence the WARNING channel exists to end — "
        "a shorter fault would be reported once and then look resolved"
    )


def test_an_intermittent_fault_is_throttled_like_a_sustained_one(monkeypatch, caplog):
    """A success clears the COUNTER, never the window.

    The first version popped the whole bookkeeping entry on a successful
    publish, which re-armed the immediate warning. A registry refusing writes
    on ALTERNATING sweeps therefore warned on every failing sweep. Measured
    over 20 sweeps against the eight shipped components: **80** lines
    intermittent against **8** continuous — and the publisher runs once per
    liveness probe, so that is the outage-long flood the throttle exists to
    prevent, reached by the fault mode nobody tested.

    The bound is lines per component per window, whatever the fault does in
    between. 40 sweeps here rather than 20, so the failure is not a near miss.
    """
    monitor = ComponentHealthMonitor()
    registry = _RefusingRegistry()
    _install_registry(monkeypatch, registry)

    with caplog.at_level(logging.WARNING, logger=component_monitor_module.__name__):
        for sweep in range(40):
            registry.failing = sweep % 2 == 0
            monitor.get_overall_health_status()

    assert len(_publish_warnings(caplog)) == len(monitor.component_health)
    assert _recovery_warnings(caplog) == []


def test_a_recovery_reports_how_many_publishes_the_throttle_hid(monkeypatch, caplog):
    """Clearing the counter must not DISCARD it.

    The first version popped the entry, so a recovery dropped the pending
    count on the floor: 50 failing sweeps, one success and one failure
    produced 16 lines and not one `(N further failures suppressed)` — while
    the docstring and the operations README both promised the count travels
    "so the throttle cannot hide the scale". The recovery is where that
    promise has to be kept, because a fault that heals is the case in which
    nothing else will ever report it.

    The recovery line is a WARNING like the one it closes: a deployment that
    emits the opening line and not the closing one is worse informed than one
    that emits neither.
    """
    monitor = ComponentHealthMonitor()
    registry = _RefusingRegistry()
    _install_registry(monkeypatch, registry)

    with caplog.at_level(logging.WARNING, logger=component_monitor_module.__name__):
        for _ in range(4):  # one reported, three suppressed
            monitor.get_overall_health_status()
        caplog.clear()

        registry.failing = False
        _window_has_passed(monkeypatch)
        monitor.get_overall_health_status()

    recovered = _recovery_warnings(caplog)
    assert len(recovered) == len(monitor.component_health)
    assert all("after 3 failed publish(es)" in message for message in recovered)
    assert {record.levelname for record in caplog.records} == {"WARNING"}
    assert _publish_warnings(caplog) == []


def test_a_healthy_publisher_says_nothing_and_keeps_no_backlog(monkeypatch, caplog):
    """The common case costs nothing — and a NEW outage is still immediate.

    Keeping the window across a success is what fixes the intermittent
    flood; the risk it introduces is the opposite one, a genuinely new outage
    being throttled against a window nothing has touched for hours. It is not,
    because a success with nothing outstanding leaves the window alone.
    """
    monitor = ComponentHealthMonitor()
    registry = _RefusingRegistry(failing=False)
    _install_registry(monkeypatch, registry)

    with caplog.at_level(logging.WARNING, logger=component_monitor_module.__name__):
        for _ in range(5):
            monitor.get_overall_health_status()
        assert _publish_warnings(caplog) == []
        assert _recovery_warnings(caplog) == []
        assert monitor._gauge_publish_warn_state == {}

        registry.failing = True
        monitor.get_overall_health_status()

    assert len(_publish_warnings(caplog)) == len(monitor.component_health)


def test_the_reporter_cannot_reach_the_caller_on_either_path(monkeypatch):
    """Neither arm of `_report_gauge_publish_outcome` may escape.

    ⚠️ The failing half OVERLAPS `test_a_broken_gauge_never_costs_the_health_read`
    above — same registry, same two assertions. An earlier docstring here
    justified the duplication by claiming that test "does not reach past the
    publish", which is false: it drives the same `except`. What is genuinely
    new is the SECOND half below, because the reporter is now called from the
    success path too, INSIDE the publisher's `try` — so a fault in the
    bookkeeping would be caught by the publisher's own `except` and reported
    as a failed publish that never happened.
    """
    monitor = ComponentHealthMonitor()
    registry = _RefusingRegistry()
    _install_registry(monkeypatch, registry)
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY

    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]

    # The success path, which the publish itself cannot exercise.
    registry.failing = False
    status, summary = monitor.get_overall_health_status()
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]


def test_a_refused_publish_freezes_the_series_rather_than_removing_it(
    published, monkeypatch
):
    """The fact the operations README now states, pinned as a measurement.

    It is the most consequential thing about this whole metric and the least
    obvious: a metrics export that refuses writes does not make the series
    vanish, it leaves the child at whatever it last held. So during a fatal
    outage the page rule reads `3` and does not fire, and there is no gap for
    an operator to notice — which is why the log line says "absent or frozen"
    and why the README tells them to grep for it instead of hunting a hole.

    The narrow label-flip case IS a removal, and that is the only one.
    """
    monitor = ComponentHealthMonitor()
    _all(monitor, HealthStatus.HEALTHY)
    monitor.get_overall_health_status()
    assert _grades(published())["database"] == 3.0

    # The `published` fixture reads its own registry directly, so replacing
    # the shim's export below does not hide what the real gauge still holds.
    class _RefusingWrapper:
        def labels(self, **_kwargs):
            raise RuntimeError("metrics registry broken")

        def remove(self, *_args):
            raise RuntimeError("metrics registry broken")

    monkeypatch.setattr(
        "faultmaven.infrastructure.shims.component_health_status",
        _RefusingWrapper(),
        raising=False,
    )
    monitor.component_health["database"].status = HealthStatus.UNHEALTHY
    status, summary = monitor.get_overall_health_status()

    # `/health` is right about the outage...
    assert status is HealthStatus.UNHEALTHY
    assert summary["fatal_unhealthy"] == ["database"]
    # ...and the gauge is still serving `healthy` off the same registry.
    assert _grades(published())["database"] == 3.0
    assert _fatal_and_down(published()) == set(), "the page rule does not fire"
