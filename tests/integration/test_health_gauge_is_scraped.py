"""The fatal-health gauge reaches a real `/metrics` scrape (#1547).

Everything else about this metric can be right and still deliver nothing: the
derivation is pinned by
``tests/unit/infrastructure/health/test_component_health_gauge.py``, but the
composition root decides whether it is ever published, and `/metrics` is
mounted from a setting read at **import** time. So this is a subprocess — the
only way to exercise the shipped registration rather than a rebuilt copy of it.

What it proves is the end of the chain #1547 is about: with the deployment's
own configuration, a database the monitor reports as down is visible to
something a Prometheus rule can read, and it says the same thing `/health`'s
body says.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# `prometheus-client` is a cloud extra (`requirements/cloud.txt` only), and
# without it the shim hands out NoOpMetrics and nothing is exported at all.
# The metric is a cloud-deployment surface, so skipping here is the honest
# answer on the standalone leg rather than a gap being papered over.
pytest.importorskip("prometheus_client")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _paging(series: dict) -> set:
    """The rule `docs/operations/monitoring/README.md` publishes, verbatim:

    component_health_status{fatal="true", fails_per_replica="false"} == 1
    """
    return {
        key.split("|")[0]
        for key, value in series.items()
        if key.split("|")[1] == "true" and key.split("|")[2] == "false" and value == 1.0
    }


def _fatal_and_down(series: dict) -> set:
    """`{fatal="true"} == 1` — the gauge's spelling of `fatal_unhealthy`.

    A superset of `_paging`; the two coincide here only because `database` is
    the sole fatal component. Kept separate so the comparison against the body
    is made against the set the body actually means.
    """
    return {
        key.split("|")[0]
        for key, value in series.items()
        if key.split("|")[1] == "true" and value == 1.0
    }


# Run in-process in the child, because the app is built at import time and the
# settings that mount /metrics and arm the metric shim are read then.
_CHILD = r"""
import json
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from faultmaven.main import app
from faultmaven.infrastructure.health.component_monitor import (
    HealthStatus,
    component_monitor,
)

async def _no_io():
    return component_monitor.component_health

component_monitor.check_all_components = _no_io
for health in component_monitor.component_health.values():
    health.status = HealthStatus.HEALTHY
component_monitor.component_health["database"].status = HealthStatus.UNHEALTHY

def _series(response):
    out = {}
    for family in text_string_to_metric_families(response.text):
        if family.name != "component_health_status":
            continue
        for sample in family.samples:
            out[
                "|".join(
                    (
                        sample.labels["component"],
                        sample.labels["fatal"],
                        sample.labels["fails_per_replica"],
                    )
                )
            ] = sample.value
    return out

client = TestClient(app)

# ORDER IS THE TEST. Prometheus scrapes /metrics; it never calls /health.
# Scraping first is what makes this measure the registered hook instead of a
# side effect of the health read — with /health called first the assertions
# below pass with no hook registered at all.
metrics_cold = client.get("/metrics")
health = client.get("/health")
metrics_warm = client.get("/metrics")
detail = client.get("/health/components/database")

print("@@RESULT@@" + json.dumps({
    "health_code": health.status_code,
    "health_status": health.json()["status"],
    "fatal_unhealthy": health.json()["summary"]["fatal_unhealthy"],
    "component_fails_per_replica": health.json()["components"]["database"][
        "fails_per_replica"
    ],
    "detail_fails_per_replica": detail.json()["metrics"]["fails_per_replica"],
    "detail_health_keys": sorted(detail.json()["health"].keys()),
    "metrics_code": metrics_cold.status_code,
    "series": _series(metrics_cold),
    "series_after_health": _series(metrics_warm),
}))
"""


@pytest.fixture(scope="module")
def scraped() -> dict:
    """Boot the app as it ships with metrics on, and read both surfaces."""
    env = dict(os.environ)
    env.update(
        {
            "METRICS_EXPORTER": "prometheus_http",
            # Read when the shim module is imported: without it every metric
            # in the process is a NoOpMetric and /metrics is empty but 200 —
            # which is exactly the "correct and unread" shape being closed.
            "ENABLE_METRICS": "true",
            "SKIP_SERVICE_CHECKS": "true",
            "OPIK_ENABLED": "false",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    marker = "@@RESULT@@"
    assert marker in completed.stdout, (
        f"child produced no result (rc={completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout[-4000:]}\n"
        f"--- stderr ---\n{completed.stderr[-4000:]}"
    )
    return json.loads(completed.stdout.split(marker, 1)[1].splitlines()[0])


def test_a_scrape_alone_publishes_the_gauge(scraped):
    """The registered hook, measured before anything has called `/health`.

    Positive control and wiring check in one: Prometheus reads `/metrics` and
    nothing else, so a gauge that only appears once a Kubernetes probe has hit
    `/health` is a gauge that happens to work. `series` here is from the first
    scrape of the process.
    """
    assert scraped["metrics_code"] == 200
    assert scraped["series"], "no component_health_status series on a cold scrape"
    assert len(scraped["series"]) >= 8


def test_the_scrape_and_the_body_report_the_same_outage(scraped):
    """`/health` 200 with a fatal component down; `/metrics` says so too.

    This pair is the issue: the body has always been right and unread, and the
    status code has always been 200 on purpose. The alert reads the second
    line, and it has to mean the first.
    """
    assert scraped["health_code"] == 200
    assert scraped["health_status"] == "unhealthy"
    assert scraped["fatal_unhealthy"] == ["database"]

    assert _fatal_and_down(scraped["series"]) == set(scraped["fatal_unhealthy"])
    # And a scrape taken after the body was served says the same thing, so
    # the two surfaces do not merely agree once.
    assert _fatal_and_down(scraped["series_after_health"]) == set(
        scraped["fatal_unhealthy"]
    )
    assert scraped["series_after_health"] == scraped["series"]
    # The DOCUMENTED page rule fires here too: `database` is fatal and shared,
    # so no readiness probe can shed around it.
    assert _paging(scraped["series"]) == {"database"}


def test_healthy_components_are_on_the_wire_too(scraped):
    """So "everything is fine" and "nobody is scraping" are different."""
    healthy = {
        key.split("|")[0] for key, value in scraped["series"].items() if value == 3.0
    }
    assert "redis" in healthy and "vector_store" in healthy
    assert "database" not in healthy


def test_the_component_body_now_carries_fails_per_replica(scraped):
    """#1543's review: `fatal` alone never answered "will readiness 503?"."""
    assert scraped["component_fails_per_replica"] is False
    assert scraped["detail_fails_per_replica"] is False
    # Once per body: the detail route carries the pair under `metrics`, and
    # does not repeat it in the ad-hoc `health` block beside it.
    assert "fails_per_replica" not in scraped["detail_health_keys"]
