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
root. The suite's network guard is installed in the child, because the
zero-config ``local`` preset names a model server. Unless a case says
otherwise the database is in memory, so nothing is written, and the settings
sign with the secret of whichever file they read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

ROOT = Path(faultmaven.__file__).resolve().parent.parent
CHOSEN = "chosen-file-secret"
DEFAULT = "default-file-secret"
IN_MEMORY = "sqlite+aiosqlite:///:memory:"

_CHILD = """
import json, sys

from tests import network_guard

network_guard.install()

import faultmaven
assert faultmaven.__file__.startswith(sys.argv[1]), (faultmaven.__file__, sys.argv[1])
from faultmaven.config.settings import get_settings
key = get_settings().security.jwt_secret_key
print("RESULT " + json.dumps({
    "secret": key.get_secret_value() if key else None,
    "network_refusals": network_guard.drain_refusals(),
}))
"""


def _signing_secret(
    tmp_path: Path,
    environ: dict[str, str],
    dotenv: str = "",
    database_url: str = IN_MEMORY,
) -> str:
    """The secret ``get_settings()`` signs with, beside ``chosen.jwt`` and the
    shipped ``data/.jwt_secret``, under ``environ`` (in its order) and a
    ``.env`` of ``dotenv``. The working directory is ``tmp_path / "cwd"``."""
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
        [sys.executable, str(script), str(ROOT) + os.sep],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": str(ROOT),
            "AUTH_MODE": "local",
            "DATABASE_URL": database_url,
            **environ,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, result.stdout[-2000:]
    answer = json.loads(lines[-1].removeprefix("RESULT "))
    assert answer["network_refusals"] == []
    return answer["secret"]


@pytest.mark.parametrize(
    ("environ", "dotenv"),
    [
        ({"jwt_secret_file": "chosen.jwt"}, ""),
        ({}, "jwt_secret_file=chosen.jwt\n"),
        ({}, "JWT_SECRET_FILE=chosen.jwt\n"),
        ({"SECURITY": '{"jwt_secret_file": "chosen.jwt"}'}, ""),
        (
            {
                "JWT_SECRET_FILE": "data/.jwt_secret",
                "SECURITY": '{"jwt_secret_file": "chosen.jwt"}',
            },
            "",
        ),
    ],
    ids=[
        "lowercase-environment",
        "lowercase-dotenv",
        "uppercase-dotenv",
        "nested-security-json",
        "nested-security-json-over-the-flat-name",
    ],
)
def test_the_variable_names_the_file_in_any_letter_case_and_source(
    tmp_path, environ, dotenv
):
    assert _signing_secret(tmp_path, environ, dotenv) == CHOSEN


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {"JWT_SECRET_FILE": "data/.jwt_secret", "jwt_secret_file": "chosen.jwt"},
            CHOSEN,
        ),
        (
            {"jwt_secret_file": "chosen.jwt", "JWT_SECRET_FILE": "data/.jwt_secret"},
            DEFAULT,
        ),
    ],
    ids=["lowercase-last", "uppercase-last"],
)
def test_of_two_spellings_the_last_in_the_environment_wins(tmp_path, environ, expected):
    assert _signing_secret(tmp_path, environ) == expected


def test_a_persistent_database_publishes_the_secret_at_the_chosen_path(tmp_path):
    """The write side: with nothing at a lowercase spelling's path, the secret
    generated there is published there, and the default file is untouched."""
    secret = _signing_secret(
        tmp_path,
        {"jwt_secret_file": "fresh/new.jwt"},
        database_url="sqlite+aiosqlite:///./data/faultmaven.db",
    )

    work = tmp_path / "cwd"
    assert secret and secret not in (CHOSEN, DEFAULT)
    assert (work / "fresh/new.jwt").read_text() == secret
    assert (work / "data/.jwt_secret").read_text() == DEFAULT


def test_with_nothing_set_the_shipped_default_is_read(tmp_path):
    """An existing secret at the default path is still found: no deployment's
    local secret moves."""
    assert _signing_secret(tmp_path, {}) == DEFAULT


def test_an_empty_value_names_the_working_directory_not_the_default(tmp_path):
    """``''`` is the cwd, which holds no secret file, so the process signs with
    one of its own; it never falls back to the default file."""
    secret = _signing_secret(tmp_path, {"JWT_SECRET_FILE": ""})

    assert secret and secret not in (CHOSEN, DEFAULT)
