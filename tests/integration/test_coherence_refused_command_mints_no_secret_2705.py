"""An operator command on settings the coherence gate refuses mints no secret (#2705).

Operator commands do not run the coherence gate, and the first ``get_settings()``
mints the local HS256 secret. On a cloud deployment with local auth, or
``TENANT_PROVIDER=multi`` outside cloud, ``fm-provision-service-account`` used
to leave ``data/.jwt_secret`` in its working directory before refusing. It is run
here for real, in a child, from an empty working directory with a persistent
SQLite database, so the persistent-database gate passes and the command's own
refusal answers: exit 1, a refusal and no traceback on stderr, nothing on
stdout, and nothing written. ``FAULTMAVEN_SKIP_DOTENV`` keeps every ``.env``
out of the child, and the refusal comes before the command builds anything
that connects.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.integration

TREE = Path(faultmaven.__file__).resolve().parent.parent

_CODE = (
    "import sys, faultmaven\n"
    f"assert faultmaven.__file__.startswith({str(TREE)!r}), faultmaven.__file__\n"
    "sys.argv = ['fm-provision-service-account', '-u', 'slack-agent']\n"
    "from faultmaven.cli.provision_service_account import main\n"
    "main()\n"
)


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param({"DEPLOYMENT_MODE": "cloud"}, id="cloud"),
        pytest.param(
            {"DEPLOYMENT_MODE": "standalone", "TENANT_PROVIDER": "multi"},
            id="multi-standalone",
        ),
    ],
)
def test_the_command_refuses_cleanly_and_writes_no_secret(refused, tmp_path):
    cwd = tmp_path / "run"
    cwd.mkdir()
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(TREE),
        "FAULTMAVEN_SKIP_DOTENV": "1",
        "CONFIG_PRESET": "none",
        "AUTH_MODE": "local",
        # No JWT_SECRET_KEY: the first get_settings() would mint one.
        "DATABASE_URL": "sqlite+aiosqlite:///./data/faultmaven.db",
        **refused,
    }

    completed = subprocess.run(
        [sys.executable, "-c", _CODE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=600,
    )

    detail = f"stdout:\n{completed.stdout[-2000:]}\nstderr:\n{completed.stderr[-3000:]}"
    assert completed.returncode == 1, detail
    assert "AUTH_MODE is 'local', not 'oauth'" in completed.stderr, detail
    assert completed.stdout == "", detail
    assert "Traceback" not in completed.stderr, detail
    assert "Uncaught exception" not in completed.stderr, detail
    assert sorted(str(p.relative_to(cwd)) for p in cwd.rglob("*")) == [], detail
