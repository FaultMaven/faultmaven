"""``get_settings()`` in a cold interpreter: the zero-config decision end to end.

``detect_zero_config_preset`` runs INSIDE ``get_settings()``, before any
settings object exists, and answers from the provider registry, which it
imports at that moment (the registry pulls in every provider adapter). That is
safe only while nothing on that import chain asks for the settings at import
time: ``get_settings()`` would then re-enter itself with ``_settings_instance``
still unset. The unit tests cannot see that, because their process has long
since imported all of it, so each case here is a fresh interpreter that imports
``faultmaven.config.settings`` first and calls ``get_settings()`` second, as
``python -m faultmaven.server`` does (#1984, #1985).

The child gets no environment of the parent's, so a CI profile's ``REDIS_HOST``
or a provider key on a developer's box cannot decide the outcome, and it runs in
an empty working directory so the settings' ``.env`` is the one the case writes.
``tests/conftest.py`` is not loaded in a child, so ``.env`` is read for real.

The child is a script file, not ``python -c``. ``get_settings()``' bare
``load_dotenv()`` walks up from ``settings.py`` for a script or ``-m`` launch,
as the server is, but from the WORKING DIRECTORY when the interpreter is
interactive, and ``-c`` counts as interactive (``__main__`` has no
``__file__``). Under ``-c`` it would load the case's own ``.env`` into the
environment before the preset check, and the case about a file only the
settings' own source reads would pass for the wrong reason.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import faultmaven

pytestmark = pytest.mark.unit

ROOT = str(Path(faultmaven.__file__).resolve().parents[1])

#: Fails the child, not this file, when the editable install (or anything else on
#: the path) hands it a different checkout than the one under test.
_CHILD = """
import json, os, sys

root = sys.argv[1]
from faultmaven.config.settings import get_settings
import faultmaven

assert faultmaven.__file__.startswith(root), (faultmaven.__file__, root)
settings = get_settings()
print("RESULT " + json.dumps({
    "preset": os.environ.get("CONFIG_PRESET"),
    "provider": settings.llm.provider.value,
    "port": settings.server.port,
}))
"""


def _get_settings_cold(tmp_path: Path, environ: dict[str, str], dotenv: str | None):
    work = tmp_path / "cwd"
    work.mkdir()
    if dotenv is not None:
        (work / ".env").write_text(dotenv)
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    result = subprocess.run(
        [sys.executable, str(script), ROOT],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": ROOT,
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


def test_a_zero_config_environment_gets_the_local_preset(tmp_path):
    assert _get_settings_cold(tmp_path, {}, None) == {
        "preset": "local",
        "provider": "local",
        "port": 8000,
    }


def test_a_configured_environment_gets_no_preset(tmp_path):
    answer = _get_settings_cold(
        tmp_path, {"GEMINI_API_KEY": "x", "CHAT_PROVIDER": "gemini"}, None
    )

    assert answer["preset"] is None
    assert answer["provider"] == "gemini"
    assert answer["port"] != 8000


def test_a_provider_named_only_in_the_working_directory_dotenv_gets_no_preset(
    tmp_path,
):
    """Probe (a) end to end: the user's ``CHAT_PROVIDER`` is the one in force."""
    answer = _get_settings_cold(
        tmp_path, {}, "GEMINI_API_KEY=x\nCHAT_PROVIDER=gemini\n"
    )

    assert answer["preset"] is None
    assert answer["provider"] == "gemini"
    assert answer["port"] != 8000


def test_skip_dotenv_gets_the_preset_for_a_key_only_the_file_holds(tmp_path):
    """The settings are built without the file, so nothing in it configures them."""
    answer = _get_settings_cold(
        tmp_path, {"FAULTMAVEN_SKIP_DOTENV": "1"}, "GEMINI_API_KEY=x\n"
    )

    assert answer["preset"] == "local"
    assert answer["provider"] == "local"
