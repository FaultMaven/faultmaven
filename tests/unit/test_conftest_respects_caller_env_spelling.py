"""The test data root leaves a caller's variable alone in any letter case (#1835).

``tests/conftest.py`` points each ``./data`` path at a root of the test
process's own unless the caller set the variable. pydantic-settings binds a
variable in any letter case and takes the last spelling, so a check of the
exact name read a caller's ``database_url`` as unset and added a
``DATABASE_URL`` that won: the run silently tested the root's SQLite file. A
spelling an ancestor test process left is replaced whichever spelling comes
last. ``JWT_SECRET_FILE`` is a settings field too (#2918), judged by the same
rule. The block runs at conftest import, so each case imports conftest in a
fresh interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from faultmaven.config.settings import env_var_spellings

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json, os
import tests.conftest
from faultmaven.config.settings import FaultMavenSettings, env_var_spellings
settings = FaultMavenSettings()
print(json.dumps({
    "url": settings.database.database_url,
    "secret_file": settings.security.jwt_secret_file,
    "spellings": env_var_spellings(os.environ, "DATABASE_URL"),
    "root": os.environ["FAULTMAVEN_TEST_DATA_ROOT"],
    "jwt_secret_file": {
        key: os.environ[key] for key in env_var_spellings(os.environ, "JWT_SECRET_FILE")
    },
}))
"""


def _conftest_under(**variables: str) -> dict:
    """What a process that imports conftest under ``variables`` resolves."""
    env = dict(os.environ)
    for name in ("DATABASE_URL", "JWT_SECRET_FILE"):
        for key in env_var_spellings(env, name):
            del env[key]
    env.update(variables)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PROJECT_ROOT), env.get("PYTHONPATH")) if part
    )
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_callers_lowercase_database_url_is_left_alone(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/caller.db"

    seen = _conftest_under(database_url=url)

    assert seen["url"] == url


def test_an_inherited_roots_url_is_replaced_in_every_spelling(tmp_path):
    inherited = str(tmp_path / "ancestor")
    stale = f"sqlite+aiosqlite:///{inherited}/faultmaven.db"

    seen = _conftest_under(
        FAULTMAVEN_TEST_DATA_ROOT=inherited, DATABASE_URL=stale, database_url=stale
    )

    assert seen["spellings"] == ["DATABASE_URL"]
    assert seen["url"] == f"sqlite+aiosqlite:///{seen['root']}/faultmaven.db"


def test_an_ancestors_spelling_is_replaced_beside_a_callers(tmp_path):
    """An ancestor's ``DATABASE_URL`` and ``JWT_SECRET_FILE`` come first and the
    caller's lowercase spellings last: no spelling is left under the ancestor's
    root, and each caller's lowercase spelling is the only one left, so it is
    the setting's value."""
    inherited = str(tmp_path / "ancestor")
    caller_url = f"sqlite+aiosqlite:///{tmp_path}/caller.db"
    caller_secret_file = str(tmp_path / "caller.jwt")

    seen = _conftest_under(
        FAULTMAVEN_TEST_DATA_ROOT=inherited,
        DATABASE_URL=f"sqlite+aiosqlite:///{inherited}/faultmaven.db",
        JWT_SECRET_FILE=os.path.join(inherited, ".jwt_secret"),
        database_url=caller_url,
        jwt_secret_file=caller_secret_file,
    )

    assert seen["jwt_secret_file"] == {"jwt_secret_file": caller_secret_file}
    assert seen["secret_file"] == caller_secret_file
    assert seen["spellings"] == ["database_url"]
    assert seen["url"] == caller_url
