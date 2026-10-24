"""``.env`` comes from the working directory and from nowhere else (#2140).

``get_settings``, ``faultmaven.main`` (at import), ``faultmaven.jobs.run.main``
and ``alembic/env.py`` load a dotenv into ``os.environ``. A bare
``load_dotenv()`` searches upward from the calling module's file, so a ``.env``
in any directory above the package reached the process (a developer's
checkout, or any directory over an installed package), even under
``FAULTMAVEN_SKIP_DOTENV``. They share one loader now, ``load_settings_dotenv``,
which reads only the file the settings name, relative to the working directory,
and never over a variable the environment sets in any letter case.

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
ABOVE_PACKAGE = "FM_2140_ABOVE_PACKAGE"
ABOVE_CWD = "FM_2140_ABOVE_CWD"
IN_MEMORY = "DATABASE_URL=sqlite+aiosqlite:///:memory:\n"

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
llm = get_settings().llm
keys = {"cohere": llm.cohere_api_key, "gemini": llm.gemini_api_key}
print("RESULT " + json.dumps({
    "environ": environ,
    "llm": {name: key.get_secret_value() if key else None for name, key in keys.items()},
}))
"""

_ENV = {"PATH": "/usr/bin:/bin"}


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
        env={**_ENV, "HOME": str(tmp_path), "PYTHONPATH": str(package_root), **environ},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, result.stdout[-2000:]
    return json.loads(lines[-1].removeprefix("RESULT "))


@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_a_dotenv_outside_the_working_directory_never_reaches_the_environment(
    tmp_path, entry
):
    """Neither search finds one: up from the package, nor up from the cwd.

    The package is installed beneath a directory holding ``.env``, and the cwd
    is an empty directory beneath another one.
    """
    (tmp_path / ".env").write_text(f"{ABOVE_CWD}=from-above-the-cwd\n")
    ancestor = tmp_path / "ancestor"
    (ancestor / "site").mkdir(parents=True)
    (ancestor / ".env").write_text(f"{ABOVE_PACKAGE}=from-above-the-package\n")
    (ancestor / "site" / "faultmaven").symlink_to(PACKAGE, target_is_directory=True)

    answer = _run(
        tmp_path, ancestor / "site", entry, {}, None, [ABOVE_PACKAGE, ABOVE_CWD]
    )

    assert answer["environ"] == {ABOVE_PACKAGE: None, ABOVE_CWD: None}


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
    assert answer["llm"]["cohere"] == "from-the-file"


def test_the_environment_wins_in_any_letter_case(tmp_path):
    """The settings bind ``gemini_api_key`` as ``GEMINI_API_KEY``, so the loader must."""
    answer = _run(
        tmp_path,
        ROOT,
        "get_settings",
        {"gemini_api_key": "from-os"},
        "GEMINI_API_KEY=from-file\n",
        ["GEMINI_API_KEY"],
    )

    assert answer["environ"] == {"GEMINI_API_KEY": None}
    assert answer["llm"]["gemini"] == "from-os"


def test_a_reference_in_the_dotenv_resolves_to_the_environment_first(tmp_path):
    """``${DB_HOST}`` means the value in force: the OS one, not the file's own."""
    answer = _run(
        tmp_path,
        ROOT,
        "get_settings",
        {"DB_HOST": "from-os"},
        "DB_HOST=from-file\n"
        "TEST_URL=postgresql://u@${DB_HOST}/db\n"
        "TEST_QUOTED='q-${DB_HOST}'\n",
        ["DB_HOST", "TEST_URL", "TEST_QUOTED"],
    )

    assert answer["environ"] == {
        "DB_HOST": "from-os",
        "TEST_URL": "postgresql://u@from-os/db",
        "TEST_QUOTED": "q-from-os",
    }


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

    assert answer["environ"] == {"COHERE_API_KEY": None}
    assert answer["llm"]["cohere"] is None


def _alembic_current(
    tmp_path: Path, environ: dict[str, str], dotenv: str | None, above: str | None
) -> subprocess.CompletedProcess:
    """``python -m alembic current`` in a directory holding this tree's alembic.

    ``script_location = alembic`` is cwd-relative, so the cwd holds symlinks to
    ``alembic/`` and ``alembic.ini``. ``data/`` exists, so the default database
    opens there when no ``DATABASE_URL`` is read.
    """
    work = tmp_path / "above" / "cwd"
    (work / "data").mkdir(parents=True)
    (work / "alembic").symlink_to(ROOT / "alembic", target_is_directory=True)
    (work / "alembic.ini").symlink_to(ROOT / "alembic.ini")
    if dotenv is not None:
        (work / ".env").write_text(dotenv)
    if above is not None:
        (work.parent / ".env").write_text(above)
    env = {**_ENV, "HOME": str(tmp_path), "PYTHONPATH": str(ROOT), **environ}
    probe = subprocess.run(
        [sys.executable, "-c", "import faultmaven; print(faultmaven.__file__)"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.stdout.startswith(str(ROOT)), (probe.stdout, probe.stderr[-2000:])
    return subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_alembic_reads_the_working_directory_dotenv(tmp_path):
    """The in-memory URL is only in the file, so its refusal shows the file was read."""
    result = _alembic_current(tmp_path, {}, IN_MEMORY, None)

    assert result.returncode == 1, result.stderr[-3000:]
    assert "Refusing to run" in result.stderr, result.stderr[-3000:]


@pytest.mark.parametrize(
    ("environ", "dotenv", "above"),
    [({"FAULTMAVEN_SKIP_DOTENV": "1"}, IN_MEMORY, None), ({}, None, IN_MEMORY)],
    ids=["skip-dotenv", "dotenv-above-the-cwd"],
)
def test_alembic_reads_no_other_dotenv(tmp_path, environ, dotenv, above):
    result = _alembic_current(tmp_path, environ, dotenv, above)

    assert result.returncode == 0, result.stderr[-3000:]
    assert "Refusing to run" not in result.stderr
