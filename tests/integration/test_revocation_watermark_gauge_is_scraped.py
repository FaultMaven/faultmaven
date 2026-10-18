"""The revocation watermark gauge is started by the lifespan and scraped (#1479).

The collector's logic is unit-tested; whether the composition root ever starts
it, whether the store's count reaches `/metrics`, and whether shutdown stops
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

The store's count is patched to a value no real store holds, so the value
asserted on the wire is the one the store reported, read through the task the
lifespan started, rather than a series that merely exists.
"""

from __future__ import annotations

import json
import math
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

#: The count the patched store reports. No empty install holds seven.
_PATCHED_COUNT = 7

_CHILD = textwrap.dedent("""
    import asyncio, json

    import httpx
    from prometheus_client.parser import text_string_to_metric_families

    from faultmaven.modules.auth.infrastructure.stores.token_revocation_store import (
        SqlTokenRevocationStore,
    )

    PATCHED_COUNT = %d
    GAUGE = "faultmaven_auth_revocation_watermarks"


    async def _patched_count(self):
        return PATCHED_COUNT


    # Before the lifespan composes the store, so the task it starts reads this.
    SqlTokenRevocationStore.count_user_watermarks = _patched_count

    from faultmaven.main import app


    def _sample(text):
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                if sample.name == GAUGE:
                    return sample.value
        return None


    async def main():
        loop = asyncio.get_running_loop()
        result = {}
        async with app.router.lifespan_context(app):
            task = getattr(app.state, "revocation_watermark_task", None)
            result["task_found"] = task is not None
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                # Wait until the gauge has been set: it starts at 0.0, and the
                # first refresh runs as soon as the task is scheduled.
                deadline = loop.time() + 10
                value = None
                while True:
                    response = await client.get("/metrics")
                    result["metrics_code"] = response.status_code
                    if response.status_code == 200:
                        value = _sample(response.text)
                    if value is not None and value != 0.0:
                        break
                    if loop.time() > deadline:
                        break
                    await asyncio.sleep(0.2)
            result["value"] = value
            result["running"] = task is not None and not task.done()
        # The context has exited and this loop still runs, so a task the
        # lifespan did not cancel is still pending here.
        result["cancelled"] = task.cancelled() if task is not None else None
        print("@@RESULT@@" + json.dumps(result))


    asyncio.run(main())
    """ % _PATCHED_COUNT)


def _boot(root: Path) -> dict:
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
        "JWT_SECRET_KEY": "watermark-gauge-probe-secret-please-ignore-01",
        "METRICS_EXPORTER": "prometheus_http",
        "ENABLE_METRICS": "true",
        "SKIP_SERVICE_CHECKS": "true",
        # Every provider variable, pinned. One provider and a dummy credential,
        # because a boot needs CHAT_PROVIDER and its key present; every other
        # key empty, which the registry reads as absent.
        "CHAT_PROVIDER": "gemini",
        "GEMINI_API_KEY": "dummy-key-for-watermark-probe",
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
    """One lifespan, shared by the module: a boot is the cost.

    Its own temp root rather than ``tmp_path``, as in
    ``test_fresh_install_boots.py``.
    """
    root = Path(tempfile.mkdtemp(prefix="fm-watermark-"))
    try:
        yield _boot(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_lifespan_starts_the_task(booted):
    assert booted["task_found"] is True, booted
    assert booted["running"] is True, booted


def test_a_scrape_carries_the_stores_count(booted):
    """The value, not the series name: a series exists from import on, at 0.0,
    and a failed count reads NaN."""
    value = booted["value"]
    assert value is not None and not math.isnan(value), booted
    assert value == float(_PATCHED_COUNT), booted


def test_shutdown_stops_the_task(booted):
    assert booted["cancelled"] is True, booted
