"""OPIK_ENABLED=false must fail closed (#1121).

Before the fix, `@opik.track` at `infrastructure/llm/router.py` and
`modules/preprocessing/classifier.py` was applied whenever the SDK was
importable, regardless of OPIK_ENABLED. The SDK then lazily built a client
from its own defaults — which point at Comet Cloud — so a self-hosted
deployment with tracing "off" emitted a continuous stream of outbound trace
batches (401) plus a ~10s `www.comet.com/is-alive/ping`.

These tests prove the positive property, not just absence of an error: with
OPIK_ENABLED unset/false, exercising both decorated call sites — including a
real `DataClassifier().classify()` — constructs NO Opik client, so nothing can
reach Comet; and `init_opik_tracing` flips the SDK's own kill switch
(is_tracing_active() becomes False even if the SDK already cached True).

They assert that property rather than the mechanism. The gate is read per
CALL, not at import, so the decorator legitimately wraps the function even
when tracing is off — an earlier revision asserted decorator identity, which
would now fail against a correctly fail-closed implementation. The enabled
control runs the SAME probe body and asserts a client IS constructed, which
is what keeps the disabled assertion load-bearing.

Each probe runs in a SUBPROCESS (pattern of tests/unit/test_import_isolation.py)
because the SDK's tracing-active flag and client singleton are process-global
and settings are a module singleton — in-process assertions would pass or fail
depending on what earlier tests imported or cached.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]

OPIK_INSTALLED = importlib.util.find_spec("opik") is not None

# Env keys that would change what the probe observes if inherited from the
# developer's shell or CI. Removed from every probe env, then selectively
# re-added per test.
_OPIK_ENV_KEYS = (
    "OPIK_ENABLED",
    "OPIK_TRACK_DISABLE",
    "OPIK_USE_LOCAL",
    "OPIK_URL_OVERRIDE",
    "OPIK_LOCAL_URL",
    "OPIK_API_KEY",
)


def _run_probe(body: str, extra_env: dict | None = None) -> dict:
    """Run `body` in a fresh interpreter and return the JSON it writes to OUT.

    Runs in a temporary cwd so pydantic-settings cannot pick up the
    developer's `.env`; results come back through a file because the app logs
    on import.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    for key in _OPIK_ENV_KEYS:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.json"
        program = f"OUT = {str(out)!r}\n" + body
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=tmp,
            env=env,
            timeout=300,
        )
        assert result.returncode == 0, (
            f"probe subprocess failed:\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        assert out.exists(), (
            f"probe wrote no result:\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
        return json.loads(out.read_text())


# Imports the two real call sites, exercises both decorator factories on
# sentinel functions, and drives a real classify() call through the decorated
# method — then reports whether the SDK's client singleton was ever created.
#
# These assert the PROPERTY (no client is constructed, so nothing can reach
# Comet), not the implementation. The gate is evaluated per call rather than
# at import, so the decorator legitimately wraps the function even when
# disabled; asserting decorator identity would fail for a fail-closed
# implementation and would silently stop testing anything if the mechanism
# changed again.
_DISABLED_PROBE = """
import asyncio
import json

from faultmaven.infrastructure.llm.router import LLMRouter, _opik_track_llm
from faultmaven.modules.preprocessing.classifier import (
    DataClassifier,
    _opik_track_classifier,
)


async def af():
    return "sentinel"


def sf():
    return "sentinel"


decorated_async = _opik_track_llm("probe")(af)
decorated_sync = _opik_track_classifier(sf)

# The gated path must be transparent, not merely non-raising: same return
# value, no exception, whatever wrapping is in place.
async_result = asyncio.run(decorated_async())
sync_result = decorated_sync()

# And the real decorated entry point must run without touching the SDK.
classification = DataClassifier().classify(
    "app.log", "ERROR: timeout connecting to db"
)

result = {
    "async_result": async_result,
    "sync_result": sync_result,
    "classified_type": classification.data_type.value,
}

try:
    import opik
    from opik.api_objects import opik_client

    result["opik_installed"] = True
    # The SDK creates its client (batch senders + is-alive ping monitor)
    # through get_global_client(), which caches here. Untouched caches are
    # the positive proof that zero client construction happened — hence zero
    # outbound Comet traffic, which is the actual guarantee.
    result["client_constructed"] = (
        opik_client.get_current_client_raw() is not None
        or getattr(opik_client, "_global_singleton", None) is not None
    )
    # No trace or span may have been recorded either.
    result["tracing_active"] = opik.is_tracing_active()
except ImportError:
    result["opik_installed"] = False
    result["client_constructed"] = False
    result["tracing_active"] = False

with open(OUT, "w") as f:
    json.dump(result, f)
"""


def test_no_client_constructed_when_disabled():
    """OPIK_ENABLED unset (defaults false): exercising both decorated call
    sites — including a real classify() — constructs no Opik client, so
    nothing can reach Comet."""
    result = _run_probe(_DISABLED_PROBE)
    assert result["client_constructed"] is False
    assert result["async_result"] == "sentinel"
    assert result["sync_result"] == "sentinel"
    assert result["classified_type"]  # classify still returns a real result


def test_no_client_constructed_when_explicitly_false():
    """OPIK_ENABLED=false behaves identically to unset."""
    result = _run_probe(_DISABLED_PROBE, extra_env={"OPIK_ENABLED": "false"})
    assert result["client_constructed"] is False
    assert result["async_result"] == "sentinel"
    assert result["sync_result"] == "sentinel"


def test_importing_call_sites_has_no_side_effects():
    """Importing the decorated modules must not bootstrap the settings
    singleton.

    get_settings() runs load_dotenv(), applies presets that mutate os.environ,
    and writes data/.jwt_secret via ensure_local_jwt_secret_env(). An
    import-time gate read made a plain `import faultmaven...router` write a
    secret file into the process's cwd and freeze settings from the ambient
    environment — ahead of the conftest fixtures that exist to clear it. The
    probe runs in a temporary cwd, so a created file is unambiguous."""
    result = _run_probe("""
import json
import os

os.environ.pop("JWT_SECRET_KEY", None)

import faultmaven.infrastructure.llm.router  # noqa: F401
import faultmaven.modules.preprocessing.classifier  # noqa: F401

import faultmaven.config.settings as settings_module

result = {
    "settings_bootstrapped": settings_module._settings_instance is not None,
    "jwt_secret_env_set": "JWT_SECRET_KEY" in os.environ,
    "files_created": sorted(
        os.path.relpath(os.path.join(root, name), os.getcwd())
        for root, _dirs, names in os.walk(os.getcwd())
        for name in names
        if not name.endswith(".json")
    ),
}

with open(OUT, "w") as f:
    json.dump(result, f)
""")
    assert result["settings_bootstrapped"] is False
    assert result["jwt_secret_env_set"] is False
    assert result["files_created"] == []


@pytest.mark.skipif(not OPIK_INSTALLED, reason="opik SDK not installed")
def test_enabled_control_does_construct_client():
    """Positive control, on the SAME property the disabled tests assert:
    with OPIK_ENABLED=true the identical probe DOES construct a client.

    Without this, "no client was constructed" could hold because the probe
    never exercises anything. Running the same body under both settings is
    what makes the disabled assertion load-bearing.

    OPIK_URL_OVERRIDE points at a closed local port, so the client is built
    and its senders start but nothing leaves the machine — a control for an
    egress fix must not itself make the egress. The ping interval is pushed
    out for the same reason."""
    result = _run_probe(
        _DISABLED_PROBE,
        extra_env={
            "OPIK_ENABLED": "true",
            "OPIK_URL_OVERRIDE": "http://127.0.0.1:1/",
            "OPIK_CONNECTION_MONITOR_PING_INTERVAL": "999999",
        },
    )
    assert result["client_constructed"] is True
    assert result["tracing_active"] is True
    # Tracing must stay transparent to the caller in the enabled case too.
    assert result["async_result"] == "sentinel"
    assert result["sync_result"] == "sentinel"
    assert result["classified_type"]


_INIT_KILL_SWITCH_PROBE = """
import json
import os

import opik

# Evaluate BEFORE init: the SDK caches is_tracing_active() on first read, and
# with OPIK_TRACK_DISABLE unset it caches True. The fix must override that
# cached True, not merely influence a future first read.
before = opik.is_tracing_active()

from faultmaven.infrastructure.observability.tracing import init_opik_tracing

init_opik_tracing()

result = {
    "before": before,
    "after": opik.is_tracing_active(),
    "track_disable_env": os.environ.get("OPIK_TRACK_DISABLE"),
}

with open(OUT, "w") as f:
    json.dump(result, f)
"""


@pytest.mark.skipif(not OPIK_INSTALLED, reason="opik SDK not installed")
def test_init_disabled_flips_sdk_kill_switch():
    """init_opik_tracing with OPIK_ENABLED unset must turn the SDK's own
    tracing flag off — including overriding an already-cached True."""
    result = _run_probe(_INIT_KILL_SWITCH_PROBE)
    assert result["before"] is True  # the hazard existed
    assert result["after"] is False  # and init closed it
    assert result["track_disable_env"] == "true"


@pytest.mark.skipif(not OPIK_INSTALLED, reason="opik SDK not installed")
def test_init_enabled_without_url_also_disables_sdk():
    """OPIK_ENABLED=true with no endpoint configured must also fail closed:
    the decorators are live in this configuration, and without the kill
    switch the SDK would fall back to its Comet Cloud default."""
    result = _run_probe(_INIT_KILL_SWITCH_PROBE, extra_env={"OPIK_ENABLED": "true"})
    assert result["after"] is False
    assert result["track_disable_env"] == "true"


def test_deployed_configmap_keys_bind_to_settings(monkeypatch):
    """Deployment-contract pin: the OPIK keys shipped by the k8s ConfigMap
    (faultmaven-enterprise-infra kubernetes/apps/faultmaven/base/configmap.yaml)
    must keep binding to ObservabilitySettings fields. Renaming these fields
    breaks the deployment silently — the infra repo's
    scripts/apps/verify-opik-env-keys.sh guards the other direction (no key
    ships that the app does not read)."""
    from faultmaven.config.settings import ObservabilitySettings

    url = "http://opik-backend.opik-system.svc.cluster.local:8080"
    monkeypatch.setenv("OPIK_ENABLED", "true")
    monkeypatch.setenv("OPIK_URL_OVERRIDE", url)

    settings = ObservabilitySettings()

    assert settings.opik_enabled is True
    assert settings.opik_url_override == url
