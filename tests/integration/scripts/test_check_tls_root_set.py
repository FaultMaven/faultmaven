"""``scripts/check_tls_root_set.py`` in the local process (#2692, pins 1 and 3).

Every installed census client accepts a private CA given through
``TLS_EXTRA_CA_FILE`` once ``apply_root_set`` has run, and refuses it without,
measured by loopback handshake. With ``apply_root_set`` made a no-op in every
process the check starts (the behaviour before #2692), it fails, client by
client. The Test Cloud job has every client; Test Standalone has the stdlib,
aiohttp, httpx, requests, redis, gRPC and asyncpg, and reports the rest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.config.tls_root_set import EXTRA_CA_FILE_SETTING, OWNED_VARIABLES

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_tls_root_set.py"
#: Fifteen rows, two runs each, each a fresh interpreter: well over a measured
#: 35 s locally, and a hung client must not hang the job.
BUDGET_SECONDS = 600


def _check(tmp_path: Path, *, no_op: bool) -> tuple[int, list[str]]:
    owned = {*OWNED_VARIABLES, EXTRA_CA_FILE_SETTING}
    environment = {k: v for k, v in os.environ.items() if k not in owned}
    environment["FAULTMAVEN_SKIP_DOTENV"] = "1"
    if no_op:  # every process the check starts imports this first
        (tmp_path / "sitecustomize.py").write_text(
            "import faultmaven.config.tls_root_set as tls_root_set\n"
            "tls_root_set.apply_root_set = lambda: None\n"
        )
        environment["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(ROOT)])
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, timeout=BUDGET_SECONDS, env=environment,
    )  # fmt: skip
    lines = [line for line in result.stdout.splitlines() if line]
    assert any(" applied " in line for line in lines), result.stdout + result.stderr
    return result.returncode, lines


def _applied(lines: list[str]) -> dict[str, str]:
    return {
        line.split(" applied ")[0].strip(): line.split(" applied ")[1].strip()
        for line in lines
        if " applied " in line
    }


def test_without_the_set_every_installed_client_refuses_the_private_ca(tmp_path):
    code, lines = _check(tmp_path, no_op=True)
    assert code == 1, "\n".join(lines)
    for name, outcome in _applied(lines).items():
        if outcome.startswith("NOT INSTALLED") or name.startswith("httpx roots"):
            continue  # certifi can equal the OS bundle on a host
        assert outcome.startswith("FAIL"), (name, outcome)
        if "require" not in name:  # require accepts unverified, then fails
            assert "0 handshake(s)" in outcome, (name, outcome)


def test_with_the_set_every_installed_client_accepts_it(tmp_path):
    code, lines = _check(tmp_path, no_op=False)
    assert code == 0, "\n".join(lines)
    assert not [line for line in lines if " FAIL " in line], "\n".join(lines)
