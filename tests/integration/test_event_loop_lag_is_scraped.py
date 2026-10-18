"""The event-loop lag monitor is started by the lifespan and scraped (#2056).

The ticker's logic is unit-tested; whether the composition root ever starts
it, whether its histogram reaches `/metrics`, and whether shutdown stops
the task are only visible by running the lifespan. A subprocess, because the
exporter settings are read at import time.

**The child's environment is explicit**, as in ``test_fresh_install_boots.py``:
its own temp root, cwd and database, and no inherited ``os.environ``.
``faultmaven/main.py``'s ``load_dotenv`` walks up from its own file, not from
the cwd, so a checkout's ``.env`` still fills every variable the child leaves
unset. ``load_dotenv`` never overrides a variable that exists, so every
provider variable is pinned here, the keys empty, so that nothing the child
starts can reach a model server.

**The child drives the lifespan itself** (``app.router.lifespan_context``) and
scrapes through ``httpx.ASGITransport``. That is what lets it record the task's
state the moment the context exits, while its event loop still runs: once the
loop is closed every pending task is cancelled anyway, and a test reading the
task then passes with the lifespan's shutdown block deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import faultmaven

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# `prometheus-client` is a cloud extra; without it the shim hands out
# NoOpMetrics and nothing is exported, so there is no series to read.
pytest.importorskip("prometheus_client")

#: What the child prints. Anything else on stdout is log noise.
_MARKER = "@@RESULT@@"

_CHILD = textwrap.dedent("""
    import asyncio, json, os

    import httpx
    from prometheus_client.parser import text_string_to_metric_families

    from faultmaven.main import app

    SERIES = "faultmaven_event_loop_lag_seconds_count"


    def _sample(text):
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                if sample.name == SERIES:
                    return sample.value
        return None


    async def main():
        loop = asyncio.get_running_loop()
        result = {}
        async with app.router.lifespan_context(app):
            task = getattr(app.state, "loop_lag_task", None)
            result["task_found"] = task is not None
            if task is not None:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    deadline = loop.time() + 15
                    value = None
                    while True:
                        response = await client.get("/metrics")
                        result["metrics_code"] = response.status_code
                        if response.status_code == 200:
                            value = _sample(response.text)
                        if value:
                            break
                        if loop.time() > deadline:
                            break
                        await asyncio.sleep(0.2)
                result["value"] = value
            result["running"] = task is not None and not task.done()
        result["cancelled"] = task.cancelled() if task is not None else None
        print("@@RESULT@@" + json.dumps(result))


    asyncio.run(main())
    """)


def _boot(root: Path, exporter: str = "prometheus_http") -> dict:
    """Run the lifespan in a child process with a clean environment."""
    run_dir = root / "run"
    run_dir.mkdir()
    db = run_dir / "data" / "faultmaven.db"

    env = {
        "HOME": os.environ.get("HOME", str(root)),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(faultmaven.__file__).parent.parent),
        "DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "DEPLOYMENT_MODE": "standalone",
        "AUTH_MODE": "local",
        "ENVIRONMENT": "development",
        "JWT_SECRET_KEY": "loop-lag-probe-secret-please-ignore-01",
        "METRICS_EXPORTER": exporter,
        "ENABLE_METRICS": "true",
        "SKIP_SERVICE_CHECKS": "true",
        # Every provider variable, pinned. One provider and a dummy credential,
        # because a boot needs CHAT_PROVIDER and its key present; every other
        # key empty, which the registry reads as absent.
        "CHAT_PROVIDER": "gemini",
        "GEMINI_API_KEY": "dummy-key-for-loop-lag-probe",
        "CLASSIFIER_PROVIDER": "gemini",
        "SYNTHESIS_PROVIDER": "gemini",
        "MULTIMODAL_PROVIDER": "gemini",
        "CODE_PROVIDER": "gemini",
        "DA_PROVIDER": "gemini",
        "KNOWLEDGE_PROVIDER": "gemini",
        "STRUCTURED_OUTPUT_PROVIDER": "gemini",
        "STRICT_PROVIDER_MODE": "true",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "FIREWORKS_API_KEY": "",
        "COHERE_API_KEY": "",
        "HUGGINGFACE_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "GROQ_API_KEY": "",
        "LOCAL_LLM_URL": "",
        "TAVILY_API_KEY": "",
        "ENABLE_WEB_SEARCH": "false",
        "OPIK_ENABLED": "false",
        "OPIK_API_KEY": "",
        "OPIK_URL_OVERRIDE": "",
        # The lifespan preloads the embedding model; offline, as the image runs
        # it (Dockerfile), it loads from the cache and asks the hub nothing.
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=run_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    for line in completed.stdout.splitlines():
        if line.startswith(_MARKER):
            return json.loads(line[len(_MARKER) :])
    raise AssertionError(
        f"the child never reported (rc={completed.returncode})\n"
        f"--- stdout ---\n{completed.stdout[-4000:]}\n"
        f"--- stderr ---\n{completed.stderr[-4000:]}"
    )


@pytest.fixture(scope="module")
def booted() -> dict:
    """One lifespan, shared by the module: a boot is the cost."""
    root = Path(tempfile.mkdtemp(prefix="fm-looplag-"))
    try:
        yield _boot(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_lifespan_starts_the_task(booted):
    assert booted["task_found"] is True, booted
    assert booted["running"] is True, booted


def test_a_scrape_carries_observed_ticks(booted):
    value = booted["value"]
    assert value is not None and value > 0, booted


def test_shutdown_stops_the_task(booted):
    assert booted["cancelled"] is True, booted


def test_control_no_exporter_starts_no_task():
    root = Path(tempfile.mkdtemp(prefix="fm-looplag-ctl-"))
    try:
        result = _boot(root, exporter="none")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert result["task_found"] is False, result
