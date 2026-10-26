"""``/readiness`` and ``/health``: which carries the verdict in its status code.

Most of these serve the app without its lifespan, through ``ASGITransport`` on
the test's own event loop rather than a bare ``TestClient``: the app is then
never composed, so its protection middleware builds its own Redis client, and
only the loop it was built on can close it. A ``TestClient`` gives every
request a portal loop of its own.
"""

import contextlib

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from faultmaven.main import app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("uncomposed_main_app_auth_service"),
]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_readiness_endpoint_present():
    async with _client() as client:
        r = await client.get("/readiness")
    # We don't assert exact status here because env varies, but endpoint should exist
    assert r.status_code == 200
    assert "status" in r.json()


# --------------------------------------------------------------------------
# Which endpoint carries the verdict in its status code (#1515)
#
# Production pointed liveness, readiness AND startup probes at /health. A 503
# there restarts pods during a dependency outage and can strand a booting pod
# past its startup budget. So the split is: /health always 200 with an honest
# body, /readiness 503 on a readiness-fatal component. These four tests are
# the contract the infra change depends on.
# --------------------------------------------------------------------------


class _Monitor:
    """Stands in for the component monitor's readiness verdict."""

    def __init__(self, ready: bool = True, blocking=(), raises: bool = False):
        self._ready = ready
        self._blocking = list(blocking)
        self._raises = raises

    async def check_serving_readiness(self):
        if self._raises:
            raise RuntimeError("probe itself is broken")
        return self._ready, {
            "checked": list(self._blocking) or ["some_component"],
            "blocking": self._blocking,
            "components": {name: {"status": "unhealthy"} for name in self._blocking},
        }


def _patch_monitor(monkeypatch, monitor) -> None:
    monkeypatch.setattr(
        "faultmaven.infrastructure.health.component_monitor.component_monitor",
        monitor,
    )


async def test_readiness_is_503_when_a_readiness_fatal_component_is_unhealthy(
    monkeypatch,
):
    """The handler's half: an unready verdict becomes a 503 naming it.

    The component is deliberately not `database` — that one is fatal but
    shared, so it is excluded from the set this endpoint reads (#1524).
    """
    _patch_monitor(monkeypatch, _Monitor(ready=False, blocking=["local_scratch_disk"]))
    async with _client() as client:
        r = await client.get("/readiness")
    assert r.status_code == 503
    assert r.json()["status"] == "unready"
    assert r.json()["blocking"] == ["local_scratch_disk"]


async def test_readiness_fails_open_when_the_probe_itself_breaks(monkeypatch):
    """A bug in the check must not be able to empty the Service."""
    _patch_monitor(monkeypatch, _Monitor(raises=True))
    async with _client() as client:
        r = await client.get("/readiness")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


async def test_health_stays_200_when_the_system_is_unhealthy(monkeypatch):
    """The liveness surface reports in the body, never in the status code.

    Restarting a pod cannot reconnect a database, and doing it during an
    outage replaces a degraded service with a crash-looping one.
    """
    from faultmaven.infrastructure.health.component_monitor import (
        HealthStatus,
        component_monitor,
    )

    async def _all_unhealthy():
        for health in component_monitor.component_health.values():
            health.status = HealthStatus.UNHEALTHY
        return component_monitor.component_health

    monkeypatch.setattr(component_monitor, "check_all_components", _all_unhealthy)

    async with _client() as client:
        r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unhealthy"
    # The verdict is legible to whatever reads the body.
    assert body["summary"]["fatal_unhealthy"] == ["database"]


# --------------------------------------------------------------------------
# The readiness-fatal set is empty, so /readiness agrees with /health (#1524)
#
# The probe wiring is what #1524 lands: readinessProbe moves to /readiness in
# faultmaven-enterprise-infra, liveness and startup stay on /health. The set
# it reads is empty by construction — `database` is fatal but shared by every
# replica, so gating on it empties the Service instead of shedding traffic.
# These two tests are the pair: today the endpoints agree, and the day a
# genuinely per-pod component is declared they stop agreeing. Without the
# second, an empty set is indistinguishable from a feature that does nothing.
# --------------------------------------------------------------------------


def _monitor_with_everything_broken(monkeypatch, extra: str | None = None):
    """A real monitor, no I/O, every component UNHEALTHY."""
    from faultmaven.infrastructure.health.component_monitor import (
        ComponentHealthMonitor,
        HealthStatus,
    )

    monitor = ComponentHealthMonitor()
    if extra:
        monitor.register_component(extra, fatal=True, fails_per_replica=True)
    for health in monitor.component_health.values():
        health.status = HealthStatus.UNHEALTHY

    async def _already_broken(name: str):
        return monitor.component_health[name]

    async def _all_components():
        return monitor.component_health

    monkeypatch.setattr(monitor, "check_component_health", _already_broken)
    monkeypatch.setattr(monitor, "check_all_components", _all_components)
    _patch_monitor(monkeypatch, monitor)
    return monitor


async def test_readiness_agrees_with_health_while_the_fatal_set_is_empty(monkeypatch):
    """Every dependency down, including the database: both answer 200."""
    _monitor_with_everything_broken(monkeypatch)
    async with _client() as client:
        health = await client.get("/health")
        readiness = await client.get("/readiness")

    assert health.status_code == 200
    # The worst case really is represented — otherwise the agreement is vacuous.
    assert health.json()["status"] == "unhealthy"
    assert health.json()["summary"]["fatal_unhealthy"] == ["database"]

    assert readiness.status_code == 200
    # `checked: []` says "probed nothing, by design" on the wire, rather
    # than leaving it to be inferred from an empty `components`.
    assert readiness.json() == {"status": "ready", "checked": [], "components": {}}


async def test_a_per_replica_failable_component_makes_readiness_503(monkeypatch):
    """Declare one component that can fail on one pod, and the gate bites."""
    _monitor_with_everything_broken(monkeypatch, extra="local_scratch_disk")
    async with _client() as client:
        readiness = await client.get("/readiness")
        health = await client.get("/health")

    assert readiness.status_code == 503
    body = readiness.json()
    assert body["status"] == "unready"
    assert body["blocking"] == ["local_scratch_disk"]
    # The shared database is down too and is still not a reason to leave.
    assert list(body["components"]) == ["local_scratch_disk"]

    # And liveness is unmoved by it — a 503 here must never restart the pod.
    assert health.status_code == 200


def test_health_reports_no_component_figure_it_did_not_measure():
    """Guards the specific defect: constants presented as measurements.

    `total_vectors: 15000`, `active_sessions: 150`, `uptime_seconds: 86400`
    and friends were reported by probes that performed no I/O at all.

    Runs inside the lifespan on purpose. Without it the container is unwired,
    every probe answers UNKNOWN with empty metadata, and a scan for banned
    keys would pass by finding no keys at all — a test that measures nothing
    while looking green. The positive control below is what makes the scan
    mean something.
    """
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    components = r.json().get("components", {})
    assert components, "health must report its components"
    assert any(
        component.get("metadata") for component in components.values()
    ), "positive control: probes must have reported real metadata to scan"
    banned = {
        "total_vectors",
        "active_sessions",
        "uptime_seconds",
        "document_count",
        "index_size_mb",
        "search_cache_hit_rate",
        "hit_rate",
        "memory_usage_mb",
        "connected_clients",
        "traces_sent_24h",
        "export_failures",
        "pii_detection_accuracy",
        "models_loaded",
        "average_response_time",
        "rate_limit_remaining",
        "index_status",
        "collection_count",
        "active_providers",
        "failed_providers",
    }
    for name, component in components.items():
        leaked = banned & set(component.get("metadata") or {})
        assert not leaked, f"{name} reports fabricated metadata: {sorted(leaked)}"
        assert "uptime_seconds" not in component


# --------------------------------------------------------------------------
# Which components /readiness will 503 for, readable somewhere (#1547)
#
# `fatal` alone never answered that question — readiness-fatal is the
# CONJUNCTION of `fatal` and `fails_per_replica` — so a surface carrying only
# `fatal` left an operator with no way to see the set. Raised in #1543's
# review. These run on both CI legs: no Prometheus involved.
# --------------------------------------------------------------------------


async def test_health_reports_both_declarations_per_component():
    async with _client() as client:
        body = (await client.get("/health")).json()

    components = body["components"]
    assert components, "positive control: /health must report components"
    for name, component in components.items():
        assert "fatal" in component, name
        assert "fails_per_replica" in component, name
    assert components["database"]["fatal"] is True
    # Shared primary: fatal, and deliberately NOT readiness-fatal (#1524).
    assert components["database"]["fails_per_replica"] is False


async def test_the_component_detail_route_answers_within_the_sweep_budget(
    monkeypatch,
):
    """A probe that never finishes its cancellation (the database on a
    PostgreSQL that stopped answering) held this route open: it called
    ``check_component_health`` outside the sweep's backstop (#2266)."""
    import asyncio
    import copy

    from faultmaven.infrastructure.health.component_monitor import component_monitor
    from tests.unit.infrastructure.health.probe_deadlines import (
        probe_that_ignores_cancellation,
        shipped_ratio,
    )

    # The verdict is written back to the process-wide monitor; give it copies.
    monkeypatch.setitem(
        component_monitor.component_health,
        "database",
        copy.deepcopy(component_monitor.component_health["database"]),
    )
    monkeypatch.setitem(
        component_monitor.health_history,
        "database",
        list(component_monitor.health_history.get("database", [])),
    )
    shipped_ratio(monkeypatch)
    probe_that_ignores_cancellation(component_monitor, monkeypatch, "database")

    async with _client() as client:
        getting = asyncio.ensure_future(client.get("/health/components/database"))
        done, _ = await asyncio.wait({getting}, timeout=5)
        if not done:
            getting.cancel()
        assert done, "the component route outlasted the sweep budget"
        body = getting.result().json()

    assert body["health"]["status"] == "unhealthy"
    assert "sweep budget" in body["health"]["last_error"]


async def test_the_component_detail_route_reports_the_pair_exactly_once():
    async with _client() as client:
        body = (await client.get("/health/components/database")).json()

    # The pair lives with the other per-component metrics...
    assert body["metrics"]["fatal"] is True
    assert body["metrics"]["fails_per_replica"] is False
    # ...and is not serialised a second time in the same body. Two copies of
    # one declaration in one response is how the copies come to disagree.
    assert "fails_per_replica" not in body["health"]
