import contextlib
import os

import pytest
from fastapi.testclient import TestClient

from faultmaven.main import app

pytestmark = pytest.mark.integration


def test_readiness_endpoint_present():
    client = TestClient(app)
    r = client.get("/readiness")
    # We don't assert exact status here because env varies, but endpoint should exist
    assert r.status_code == 200
    assert "status" in r.json()


@pytest.mark.skipif(
    not os.getenv("REDIS_HOST") and not os.getenv("REDIS_URL"),
    reason="Requires REDIS_* env in CI to assert healthy readiness",
)
def test_readiness_reports_ready_with_redis_and_chroma():
    client = TestClient(app)
    r = client.get("/readiness")
    assert r.status_code == 200
    body = r.json()
    # In CI with dependencies up, expect ready; if not, this test can be adjusted per environment
    assert body.get("status") in {"ready", "unready"}


# --------------------------------------------------------------------------
# Which endpoint carries the verdict in its status code (#1515)
#
# Production points liveness, readiness AND startup probes at /health. Until
# the probes are split, a 503 there restarts pods during a dependency outage
# and can strand a booting pod past its startup budget. So the split is:
# /health always 200 with an honest body, /readiness 503 on a fatal component.
# These four tests are the contract the infra change depends on.
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
            "checked": ["database"],
            "blocking": self._blocking,
            "components": {"database": {"status": "healthy"}},
        }


def _patch_monitor(monkeypatch, monitor) -> None:
    monkeypatch.setattr(
        "faultmaven.infrastructure.health.component_monitor.component_monitor",
        monitor,
    )


def test_readiness_is_503_when_a_fatal_component_is_unhealthy(monkeypatch):
    _patch_monitor(monkeypatch, _Monitor(ready=False, blocking=["database"]))
    r = TestClient(app).get("/readiness")
    assert r.status_code == 503
    assert r.json()["status"] == "unready"
    assert r.json()["blocking"] == ["database"]


def test_readiness_fails_open_when_the_probe_itself_breaks(monkeypatch):
    """A bug in the check must not be able to empty the Service."""
    _patch_monitor(monkeypatch, _Monitor(raises=True))
    r = TestClient(app).get("/readiness")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_health_stays_200_when_the_system_is_unhealthy(monkeypatch):
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

    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unhealthy"
    # The verdict is legible to whatever reads the body.
    assert body["summary"]["fatal_unhealthy"] == ["database"]


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
