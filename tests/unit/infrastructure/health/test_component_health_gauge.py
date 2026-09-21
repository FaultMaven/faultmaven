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


def _shipped_ratio(monkeypatch, *, probe: float = 0.15, sweep: float = 0.30) -> None:
    """Scale both deadlines while KEEPING the shipped ordering, probe < sweep.

    Setting only the sweep budget — to 0.05, against a per-probe deadline left
    at 3.0 — inverts what ships (4.0 > 3.0) and proves the arm in a
    configuration that cannot occur. Magnitude is scaled so the test is fast;
    the ordering is the thing under test and is preserved.
    """
    assert probe < sweep, "the shipped ordering is per-probe deadline < sweep budget"
    monkeypatch.setattr(component_monitor_module, "_PROBE_TIMEOUT_SECONDS", probe)
    monkeypatch.setattr(
        component_monitor_module, "_ALL_COMPONENTS_TIMEOUT_SECONDS", sweep
    )


def _probe_that_ignores_cancellation(monitor, monkeypatch, name: str) -> None:
    """`name`'s probe swallows its first cancellation; everything else is fine.

    The only route to the sweep-budget arm at the shipped ratio, and the case
    `check_all_components`' own docstring names the budget a backstop for: "a
    probe that does not observe cancellation promptly". The per-probe deadline
    fires first and is ignored, so the task is still pending when the sweep
    budget expires.
    """

    async def _probe(component_name: str) -> Dict[str, Any]:
        if component_name == name:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)  # the sweep's cancel ends this one
        return {"status": HealthStatus.HEALTHY, "metadata": {}}

    monkeypatch.setattr(monitor, "_perform_health_check", _probe)


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
    _shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()
    _probe_that_ignores_cancellation(monitor, monkeypatch, "database")

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
    _shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()
    _probe_that_ignores_cancellation(monitor, monkeypatch, "database")

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
    _shipped_ratio(monkeypatch)
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
    _shipped_ratio(monkeypatch)
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
    _shipped_ratio(monkeypatch)
    monitor = ComponentHealthMonitor()
    _probe_that_ignores_cancellation(monitor, monkeypatch, "database")

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
