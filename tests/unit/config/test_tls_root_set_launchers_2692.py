"""Every core launcher and entrypoint applies the TLS root set before any client
is imported (#2692, pin 2).

Each case is a fresh interpreter with no owned variable inherited and no
``.env`` read. A recording stand-in replaces ``apply_root_set`` before the
entrypoint is imported, and records which census client modules were already
imported when it ran.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import faultmaven
from faultmaven.config.tls_root_set import EXTRA_CA_FILE_SETTING, OWNED_VARIABLES

pytestmark = pytest.mark.unit

ROOT = Path(faultmaven.__file__).resolve().parents[1]
CENSUS_CLIENTS = (
    "aiohttp", "httpx", "requests", "urllib3", "botocore", "grpc", "redis",
    "asyncpg", "psycopg2", "litellm", "sentry_sdk", "workos", "opik", "chromadb",
    "huggingface_hub",
)  # fmt: skip
SCRIPTS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
ENTRYPOINTS = {**SCRIPTS, "jobs-runner": "faultmaven.jobs.run:main"}

_RECORDER = f"""
import json, sys
import faultmaven.config.tls_root_set as tls_root_set
calls = []
def record():
    calls.append(sorted(set({CENSUS_CLIENTS!r}) & set(sys.modules)))
tls_root_set.apply_root_set = record
import atexit
atexit.register(lambda: print("CALLS " + json.dumps(calls)))
"""


def _environment(**extra: str) -> dict:
    owned = {name.upper() for name in (*OWNED_VARIABLES, EXTRA_CA_FILE_SETTING)}
    environment = {k: v for k, v in os.environ.items() if k.upper() not in owned}
    environment.update(FAULTMAVEN_SKIP_DOTENV="1", PYTHONPATH=str(ROOT), **extra)
    return environment


def _calls(code: str, **extra: str) -> list:
    result = subprocess.run(
        [sys.executable, "-c", _RECORDER + code],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env=_environment(**extra),
    )  # fmt: skip
    line = next((x for x in result.stdout.splitlines() if x.startswith("CALLS ")), None)
    assert line, result.stdout + result.stderr
    return json.loads(line.removeprefix("CALLS "))


@pytest.mark.parametrize("name", sorted(ENTRYPOINTS))
def test_each_entrypoint_applies_the_set_before_any_client_is_imported(name):
    module, function = ENTRYPOINTS[name].split(":")
    code = (
        f"sys.argv = [{name!r}, '--help']\n"
        f"from {module} import {function}\n"
        f"try:\n    {function}()\nexcept SystemExit:\n    pass\n"
    )
    assert _calls(code) == [[]]


def test_the_migration_process_applies_the_set_before_any_client_is_imported(
    tmp_path,
):
    """``upgrade head --sql`` executes ``alembic/env.py``; ``--help`` would not."""
    code = (
        "from alembic import command\n"
        "from alembic.config import Config\n"
        "command.upgrade(Config('alembic.ini'), 'head', sql=True)\n"
    )
    database = f"sqlite:///{tmp_path / 'scratch.db'}"
    assert _calls(code, DATABASE_URL=database) == [[]]


def test_the_demo_seeder_applies_the_set_before_any_client_is_imported():
    code = (
        "import asyncio\nfrom demo.seed_demo_data import main\n"
        "tls_root_set.apply_root_set = lambda: (record(), sys.exit(0))\n"
        "asyncio.run(main())\n"
    )
    assert _calls(code) == [[]]


def test_run_server_applies_the_set_before_aiohttp_builds_its_context(tmp_path):
    """aiohttp builds its verified context when it is imported, which uvicorn
    does when it imports the app: the extra CA must be in it."""
    from tests.unit.config.test_tls_root_set_2692 import EXTRA, _digest

    (tmp_path / "extra-ca.pem").write_bytes(EXTRA)
    code = """
import hashlib, sys
from unittest import mock
def serve(app, **options):
    import faultmaven.main  # what uvicorn's import of the app string does
    import aiohttp.connector
    context = aiohttp.connector._SSL_CONTEXT_VERIFIED
    digests = [hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(True)]
    print("DIGESTS " + " ".join(digests))
with mock.patch("uvicorn.run", serve):
    from faultmaven.server import run_server
    run_server()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
        env=_environment(
            TLS_EXTRA_CA_FILE=str(tmp_path / "extra-ca.pem"),
            TMPDIR=str(tmp_path),
            RELOAD="false", WORKERS="1", CONFIG_PRESET="none",
            SKIP_SERVICE_CHECKS="true",
        ),
    )  # fmt: skip
    line = next(
        (x for x in result.stdout.splitlines() if x.startswith("DIGESTS ")), None
    )
    assert line, result.stdout[-2000:] + result.stderr[-4000:]
    assert _digest(EXTRA) in line.split()
