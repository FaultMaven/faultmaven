"""The local JWT secret's file is a setting, bound in any letter case (#2918).

``resolve_local_jwt_secret`` took the file's path from
``os.environ.get("JWT_SECRET_FILE")``: the exact name, outside the settings
layer. Every settings field binds its variable in any letter case, from the
environment and from ``.env``, so a lowercase ``jwt_secret_file`` was silently
ignored and the default ``data/.jwt_secret`` signed instead. The path is
``SecuritySettings.jwt_secret_file`` now.

Each case runs ``get_settings()`` in a fresh interpreter with only the variables
it names (``env -i``) and a working directory of its own: ``tests/conftest.py``
patches dotenv away in-process and moves the default under the session's data
root. The database is in memory, so nothing is written, and the settings sign
with the secret of whichever file they read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

ROOT = Path(faultmaven.__file__).resolve().parent.parent
CHOSEN = "chosen-file-secret"
DEFAULT = "default-file-secret"

_CHILD = """
import json, sys

import faultmaven
assert faultmaven.__file__.startswith(sys.argv[1]), (faultmaven.__file__, sys.argv[1])
from faultmaven.config.settings import get_settings
key = get_settings().security.jwt_secret_key
print("RESULT " + json.dumps(key.get_secret_value() if key else None))
"""


def _signing_secret(tmp_path: Path, environ: dict[str, str], dotenv: str = "") -> str:
    """The secret ``get_settings()`` signs with, beside ``chosen.jwt`` and the
    shipped ``data/.jwt_secret``, under ``environ`` and a ``.env`` of ``dotenv``."""
    work = tmp_path / "cwd"
    (work / "data").mkdir(parents=True)
    for path, value in (
        (work / "chosen.jwt", CHOSEN),
        (work / "data/.jwt_secret", DEFAULT),
    ):
        path.write_text(value)
        path.chmod(0o600)
    (work / ".env").write_text(dotenv)
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    result = subprocess.run(
        [sys.executable, str(script), str(ROOT)],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": str(ROOT),
            "AUTH_MODE": "local",
            "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            **environ,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, result.stdout[-2000:]
    return json.loads(lines[-1].removeprefix("RESULT "))


@pytest.mark.parametrize(
    ("environ", "dotenv"),
    [
        ({"jwt_secret_file": "chosen.jwt"}, ""),
        ({}, "jwt_secret_file=chosen.jwt\n"),
        ({}, "JWT_SECRET_FILE=chosen.jwt\n"),
    ],
    ids=["lowercase-environment", "lowercase-dotenv", "uppercase-dotenv"],
)
def test_the_variable_names_the_file_in_any_letter_case_and_source(
    tmp_path, environ, dotenv
):
    assert _signing_secret(tmp_path, environ, dotenv) == CHOSEN


def test_with_nothing_set_the_shipped_default_is_read(tmp_path):
    """An existing secret at the default path is still found: no deployment's
    local secret moves."""
    assert _signing_secret(tmp_path, {}) == DEFAULT


def test_an_empty_value_names_the_working_directory_not_the_default(tmp_path):
    """``''`` is the cwd, which holds no secret file, so the process signs with
    one of its own; it never falls back to the default file."""
    secret = _signing_secret(tmp_path, {"JWT_SECRET_FILE": ""})

    assert secret and secret not in (CHOSEN, DEFAULT)
