"""Presets decide from the view the settings read, not from ``os.environ`` (#2008).

``FaultMavenSettings`` reads the process environment OVER the dotenv file in the
working directory. Every decision ``presets.py`` takes before the settings exist
(which preset, whether a key is already set, what is missing) must read that
same view, or a value written only in the file is invisible to it: the preset is
not applied, or its default lands in ``os.environ`` and beats the operator's own
file.

Each case is a fresh interpreter run from a script file in an empty working
directory holding the case's ``.env``: a child because ``tests/conftest.py``
patches the dotenv away in-process. The presets are asked before
``get_settings()`` has loaded that file into the environment (``name_before``,
``errors``), so only the settings' own view can show it to them.
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

_CHILD = """
import json, os, sys

root = sys.argv[1]
import faultmaven
assert faultmaven.__file__.startswith(root), (faultmaven.__file__, root)
from faultmaven.config import presets
from faultmaven.config.settings import get_settings

name_before = presets.get_current_preset_name()
errors = presets.validate_preset_requirements()
settings = get_settings()
print("RESULT " + json.dumps({
    "name_before": name_before,
    "errors": errors,
    "preset_after": os.environ.get("CONFIG_PRESET"),
    "local_model": settings.llm.local_model,
    "environ_local_model": os.environ.get("LOCAL_LLM_MODEL"),
    "port": settings.server.port,
}))
"""


def _run(tmp_path: Path, environ: dict[str, str], dotenv: str) -> dict:
    work = tmp_path / "cwd"
    work.mkdir()
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


def test_a_preset_named_only_in_the_working_directory_dotenv_is_applied(tmp_path):
    # ``standard`` carries PORT=8000 and ENVIRONMENT=production, neither of which
    # the settings default to; ``CHAT_PROVIDER=openai`` keeps the zero-config
    # check from choosing a preset for us.
    answer = _run(
        tmp_path,
        {},
        "CONFIG_PRESET=standard\nCHAT_PROVIDER=openai\nOPENAI_API_KEY=x\n",
    )

    assert answer["name_before"] == "standard"
    assert answer["port"] == 8000


def test_a_value_only_in_the_dotenv_beats_the_preset_default(tmp_path):
    """The ``local`` preset defaults LOCAL_LLM_MODEL to llama2; the file says otherwise.

    ``get_settings()`` loads the file into the environment before the preset is
    applied, and ``LLMSettings`` reads the environment.
    """
    answer = _run(
        tmp_path, {"CONFIG_PRESET": "local"}, "LOCAL_LLM_MODEL=from-the-file\n"
    )

    assert answer["local_model"] == "from-the-file"
    # The preset did not write its own default over the operator's file.
    assert answer["environ_local_model"] == "from-the-file"


def test_skip_dotenv_ignores_the_file_for_the_preset_too(tmp_path):
    answer = _run(
        tmp_path,
        {"CONFIG_PRESET": "local", "FAULTMAVEN_SKIP_DOTENV": "1"},
        "LOCAL_LLM_MODEL=from-the-file\nCONFIG_PRESET=standard\n",
    )

    assert answer["name_before"] == "local"
    assert answer["local_model"] == "llama2"  # the preset's default; the file is out


def test_skip_dotenv_hides_a_preset_named_only_in_the_file(tmp_path):
    answer = _run(
        tmp_path,
        {"FAULTMAVEN_SKIP_DOTENV": "1"},
        "CONFIG_PRESET=standard\nCHAT_PROVIDER=openai\nOPENAI_API_KEY=x\n",
    )

    assert answer["name_before"] is None


def test_requirements_are_judged_on_the_provider_the_dotenv_names(tmp_path):
    answer = _run(tmp_path, {"CONFIG_PRESET": "standard"}, "CHAT_PROVIDER=gemini\n")

    assert answer["errors"] == [
        "Missing required configuration 'GEMINI_API_KEY' for provider 'gemini' "
        "in preset 'standard'"
    ]
