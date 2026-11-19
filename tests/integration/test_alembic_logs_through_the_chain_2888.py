"""Alembic logs through the API's chain on stderr (#2888, principles §7).

``alembic/env.py`` installs ``install_log_handler`` before anything can log and
again after ``fileConfig`` (whose levels it keeps), so a migration's records
render as the API's and a job's do, and an exception renders as class names
and frames with no message. Stdout, where ``--sql`` writes the script, holds no
log line.

The child runs a real offline migration of the first revision through
alembic's command API, then logs a chained exception whose marker it is handed
at run time (``chained_2888`` says why the marker is never written in source).
"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.config.settings import set_env_var
from tests.unit.infrastructure.logging.chained_2888 import LINKS

pytestmark = [pytest.mark.integration, pytest.mark.security]

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CHILD = """
import logging, sys
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.unit.infrastructure.logging.chained_2888 import chained
config = Config(sys.argv[1])
command.upgrade(config, ScriptDirectory.from_config(config).get_base(), sql=True)
logging.getLogger("alembic.test").error("failed", exc_info=chained(sys.argv[2]))
"""


@pytest.mark.parametrize("log_format", ["json", "console"])
def test_a_migration_logs_through_the_chain_on_stderr(tmp_path, log_format):
    marker = secrets.token_hex(16)
    env = dict(os.environ)
    set_env_var(env, "DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    set_env_var(env, "LOG_FORMAT", log_format)
    env["FAULTMAVEN_SKIP_DOTENV"] = "1"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(PROJECT_ROOT / "alembic.ini"), marker],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    detail = f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-3000:]}"
    assert result.returncode == 0, detail
    assert "CREATE TABLE" in result.stdout, detail
    assert "alembic.runtime.migration" not in result.stdout, detail
    assert marker not in result.stderr, detail
    lines = result.stderr.strip().split("\n")
    if log_format == "json":
        records = [json.loads(line) for line in lines]
        assert records[0]["logger"] == "alembic.runtime.migration", detail
        assert records[0]["level"] == "info", detail
        rendered = records[-1]["frames"]
    else:
        assert lines[0].endswith(" [alembic.runtime.migration]"), detail
        rendered = result.stderr
    for link in LINKS:
        assert link in rendered, detail
