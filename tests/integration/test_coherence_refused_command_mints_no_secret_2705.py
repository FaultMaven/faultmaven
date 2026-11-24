"""An operator command on settings the coherence gate refuses mints no secret (#2705).

Operator commands do not run the coherence gate, and the first ``get_settings()``
mints the local HS256 secret. On a cloud deployment with local auth, or
``TENANT_PROVIDER=multi`` outside cloud, ``fm-provision-service-account`` used
to leave ``data/.jwt_secret`` in its working directory before refusing. It is run
here for real, in a child, from an empty working directory with a persistent
SQLite database, so the persistent-database gate passes and the command's own
refusal answers: exit 1, a refusal and no traceback on stderr, nothing on
stdout, and nothing written. The positive control, a standalone single-tenant
run, still mints the secret before the same refusal, so an empty listing means
no mint, not a probe looking in the wrong place.

A command that composes the container reaches the signer, which refuses these
settings with the coherence gate's own text for the rule, never "signing key
unavailable" (which would steer an operator to set ``JWT_SECRET_KEY``).
``FAULTMAVEN_SKIP_DOTENV`` keeps every ``.env`` out of the child, and nothing in
it connects beyond its own process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.integration

TREE = Path(faultmaven.__file__).resolve().parent.parent

_CLOUD = {"DEPLOYMENT_MODE": "cloud"}
_MULTI = {"DEPLOYMENT_MODE": "standalone", "TENANT_PROVIDER": "multi"}
_CLOUD_RULE = "AUTH_MODE must be 'oauth' for cloud (got 'local')"
_MULTI_RULE = "TENANT_PROVIDER='multi' requires DEPLOYMENT_MODE=cloud"

_PROLOGUE = (
    "import sys, faultmaven\n"
    f"assert faultmaven.__file__.startswith({str(TREE)!r}), faultmaven.__file__\n"
)
_PROVISION = _PROLOGUE + (
    "sys.argv = ['fm-provision-service-account', '-u', 'slack-agent']\n"
    "from faultmaven.cli.provision_service_account import main\n"
    "main()\n"
)
#: Under cloud the container refuses a local Chroma and FakeRedis before it
#: reaches the signer; these stand-ins take the place of the reachable servers
#: a cloud deployment has, so the child stays offline.
_PROMOTE = _PROLOGUE + (
    "from faultmaven.infrastructure import chroma_client, redis_client\n"
    "chroma_client.local_chroma_or_fail = lambda *a, **k: None\n"
    "redis_client.fakeredis_or_fail = (\n"
    "    lambda reason: redis_client.get_fakeredis_client())\n"
    "sys.argv = ['fm-promote-platform-admin', 'admin']\n"
    "from faultmaven.cli.promote_platform_admin import main\n"
    "main()\n"
)


def _run(code: str, settings: dict, tmp_path: Path):
    """Run ``code`` in a clean child from an empty cwd; return it and the cwd's listing."""
    cwd = tmp_path / "run"
    cwd.mkdir()
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(TREE),
        "FAULTMAVEN_SKIP_DOTENV": "1",
        "CONFIG_PRESET": "none",
        "CHAT_PROVIDER": "openai",
        "OPENAI_API_KEY": "not-a-key",
        "HF_HUB_OFFLINE": "1",
        "SKIP_SERVICE_CHECKS": "true",
        "AUTH_MODE": "local",
        # No JWT_SECRET_KEY: the first get_settings() would mint one.
        "DATABASE_URL": "sqlite+aiosqlite:///./data/faultmaven.db",
        **settings,
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=600,
    )
    return completed, sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*"))


def _detail(completed) -> str:
    return f"stdout:\n{completed.stdout[-2000:]}\nstderr:\n{completed.stderr[-3000:]}"


@pytest.mark.parametrize(
    "settings, written",
    [
        pytest.param(_CLOUD, [], id="cloud"),
        pytest.param(_MULTI, [], id="multi-standalone"),
        # The positive control: the same refusal, after the mint.
        pytest.param(
            {"DEPLOYMENT_MODE": "standalone", "TENANT_PROVIDER": "single"},
            ["data", "data/.jwt_secret"],
            id="control-standalone",
        ),
    ],
)
def test_the_command_refuses_cleanly_and_writes_no_secret(settings, written, tmp_path):
    completed, entries = _run(_PROVISION, settings, tmp_path)

    detail = _detail(completed)
    assert completed.returncode == 1, detail
    assert "AUTH_MODE is 'local', not 'oauth'" in completed.stderr, detail
    assert completed.stdout == "", detail
    assert "Traceback" not in completed.stderr, detail
    assert "Uncaught exception" not in completed.stderr, detail
    assert entries == written, detail


@pytest.mark.parametrize(
    "settings, rule",
    [
        pytest.param(_CLOUD, _CLOUD_RULE, id="cloud"),
        pytest.param(_MULTI, _MULTI_RULE, id="multi-standalone"),
    ],
)
def test_a_command_reaching_the_signer_is_refused_with_the_rule(
    settings, rule, tmp_path
):
    completed, entries = _run(_PROMOTE, settings, tmp_path)

    detail = _detail(completed)
    assert completed.returncode != 0, detail
    assert rule in completed.stderr, detail
    assert "SigningKeyUnavailableError" not in completed.stderr, detail
    assert "data/.jwt_secret" not in entries, detail
