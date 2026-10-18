"""The revocation watermark gauge is started by the lifespan and scraped (#1479).

The collector's logic is unit-tested; whether the composition root ever starts
it, and whether the series reaches `/metrics`, is only visible by booting the
app. A subprocess, because the exporter settings are read at import time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

pytest.importorskip("prometheus_client")

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = r"""
import json
from fastapi.testclient import TestClient

from faultmaven.main import app

with TestClient(app) as client:
    task = app.state.revocation_watermark_task
    running = not task.done()
    body = client.get("/metrics").text
    scraped = "faultmaven_auth_revocation_watermarks" in body
print("@@RESULT@@" + json.dumps({
    "running": running,
    "scraped": scraped,
    "cancelled_after_exit": task.cancelled() or task.done(),
}))
"""


@pytest.fixture(scope="module")
def booted() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "METRICS_EXPORTER": "prometheus_http",
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


def test_the_lifespan_starts_the_task(booted):
    assert booted["running"] is True


def test_a_scrape_carries_the_gauge(booted):
    assert booted["scraped"] is True


def test_shutdown_stops_the_task(booted):
    assert booted["cancelled_after_exit"] is True
