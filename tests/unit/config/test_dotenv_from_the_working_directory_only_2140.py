"""``.env`` comes from the working directory and from nowhere else (#2140).

``get_settings``, ``faultmaven.main`` (at import), ``faultmaven.jobs.run.main``
and ``alembic/env.py`` load a dotenv into ``os.environ``. A bare
``load_dotenv()`` searches upward from the calling module's file, so a ``.env``
in any directory above the package reached the process (a developer's
checkout, or any directory over an installed package), even under
``FAULTMAVEN_SKIP_DOTENV``. They share one loader now, ``load_settings_dotenv``,
which reads only the file the settings name, relative to the working directory.

Each case is a fresh interpreter, because ``tests/conftest.py`` patches dotenv
away in-process, and it runs a script file: under ``python -c`` the interpreter
counts as interactive, and a bare ``load_dotenv()`` then searches the working
directory instead of walking up, so the guard below would miss the walk-up.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

PACKAGE = Path(faultmaven.__file__).resolve().parent
ROOT = PACKAGE.parent
MARKER = "FM_2140_MARKER"

#: Each entry point that loads a dotenv, as the child calls it.
ENTRY_POINTS = {
    "get_settings": "from faultmaven.config.settings import get_settings\n"
    "get_settings()",
    "jobs.run.main": "from faultmaven.jobs.run import main\nmain(['--list'])",
    "import faultmaven.main": "import faultmaven.main",
}

_PREAMBLE = """
import json, os, sys

import faultmaven
assert faultmaven.__file__.startswith(sys.argv[1]), (faultmaven.__file__, sys.argv[1])
"""

#: Reads the environment the entry point left, before anything else can load.
_REPORT = """
environ = {name: os.environ.get(name) for name in sys.argv[2:]}
from faultmaven.config.settings import get_settings
key = get_settings().llm.cohere_api_key
print("RESULT " + json.dumps({
    "environ": environ,
    "cohere_api_key": key.get_secret_value() if key else None,
}))
"""


def _run(
    tmp_path: Path,
    package_root: Path,
    entry: str,
    environ: dict[str, str],
    dotenv: str | None,
    names: list[str],
) -> dict:
    """Run ``entry`` with only ``environ`` (``env -i``) in a fresh working directory."""
    work = tmp_path / "cwd"
    work.mkdir()
    if dotenv is not None:
        (work / ".env").write_text(dotenv)
    script = tmp_path / "child.py"
    script.write_text(_PREAMBLE + ENTRY_POINTS[entry] + "\n" + _REPORT)
    result = subprocess.run(
        [sys.executable, str(script), str(package_root), *names],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": str(package_root),
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


@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_a_dotenv_above_the_package_never_reaches_the_environment(tmp_path, entry):
    """An installed package beneath a directory holding ``.env``, run from elsewhere."""
    ancestor = tmp_path / "ancestor"
    (ancestor / "site").mkdir(parents=True)
    (ancestor / ".env").write_text(f"{MARKER}=from-an-ancestor\n")
    (ancestor / "site" / "faultmaven").symlink_to(PACKAGE, target_is_directory=True)

    answer = _run(tmp_path, ancestor / "site", entry, {}, None, [MARKER])

    assert answer["environ"] == {MARKER: None}


@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_the_working_directory_dotenv_is_read_and_the_environment_wins(tmp_path, entry):
    answer = _run(
        tmp_path,
        ROOT,
        entry,
        {"FM_2140_BOTH": "from-the-environment"},
        "FM_2140_BOTH=from-the-file\nCOHERE_API_KEY=from-the-file\n",
        ["FM_2140_BOTH", "COHERE_API_KEY"],
    )

    assert answer["environ"] == {
        "FM_2140_BOTH": "from-the-environment",
        "COHERE_API_KEY": "from-the-file",
    }
    assert answer["cohere_api_key"] == "from-the-file"


def test_skip_dotenv_keeps_a_credential_out_of_the_environment_and_the_settings(
    tmp_path,
):
    answer = _run(
        tmp_path,
        ROOT,
        "get_settings",
        {"FAULTMAVEN_SKIP_DOTENV": "1"},
        "COHERE_API_KEY=from-the-file\n",
        ["COHERE_API_KEY"],
    )

    assert answer == {"environ": {"COHERE_API_KEY": None}, "cohere_api_key": None}
